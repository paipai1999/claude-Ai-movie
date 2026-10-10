"""
Hardware Encoder & FFmpeg Binary Discovery (Hardware encoder နှင့် FFmpeg ရှာဖွေခြင်း)
===================================================================================
Locates a working FFmpeg executable (system / imageio / Kaggle NVENC build) and picks the
fastest H.264 encoder available (NVENC, QSV, AMF, VideoToolbox, libx264).

ဤ module သည် FFmpeg ဖိုင်ကို ရှာဖွေပြီး GPU video encoder (NVENC/QSV/AMF) ရှိ/မရှိ စစ်ဆေးကာ
အမြန်ဆုံး encoder ကို ရွေးချယ်ပေးသည်။
"""

from brain.diagnostics import log_suppressed
import os
import shutil
import sys

_DETECTED_ENCODER = None


def _ensure_linux_cuda_ld_path():
    """Ensure Linux dynamic linker finds NVIDIA CUDA & NVENC driver libraries."""
    if not sys.platform.startswith("linux"):
        return
    import glob
    import subprocess
    import shutil

    ld_candidates = [
        "/usr/lib/x86_64-linux-gnu",
        "/usr/local/cuda/lib64",
        "/usr/local/nvidia/lib",
        "/usr/local/nvidia/lib64",
        "/usr/local/cuda/targets/x86_64-linux/lib",
        "/usr/lib/wsl/lib",
        "/usr/lib64",
    ]
    cur_ld = os.environ.get("LD_LIBRARY_PATH", "")
    extra_ld = [p for p in ld_candidates if os.path.exists(p) and p not in cur_ld]
    if extra_ld:
        os.environ["LD_LIBRARY_PATH"] = ":".join(extra_ld) + ((":" + cur_ld) if cur_ld else "")

    # Auto-repair libnvidia-encode.so.1 symlink if missing but versioned library exists on Kaggle/Ubuntu
    try:
        target_link = "/usr/lib/x86_64-linux-gnu/libnvidia-encode.so.1"
        if not os.path.exists(target_link):
            matches = glob.glob("/usr/lib/x86_64-linux-gnu/libnvidia-encode.so*") + \
                      glob.glob("/usr/local/nvidia/lib64/libnvidia-encode.so*") + \
                      glob.glob("/usr/lib64/libnvidia-encode.so*")
            valid_libs = [m for m in matches if not m.endswith(".so.1") and os.path.isfile(m)]
            if valid_libs:
                target_lib = sorted(valid_libs)[-1]
                try:
                    os.symlink(target_lib, target_link)
                except Exception:
                    try:
                        shutil.copy2(target_lib, target_link)
                    except Exception as _exc:
                        log_suppressed("agents.merger.encoder._ensure_linux_cuda_ld_path", _exc)
        subprocess.run(["ldconfig"], capture_output=True, timeout=5)
    except Exception as _exc:
        log_suppressed("agents.merger.encoder._ensure_linux_cuda_ld_path", _exc)

def _auto_setup_nvenc_linux() -> str:
    """Optionally cache a BtbN NVENC build locally without modifying system binaries."""
    import subprocess
    import tarfile
    
    if not sys.platform.startswith("linux"):
        return None

    # Check if NVIDIA GPU is available
    has_nvidia = False
    try:
        chk = subprocess.run(["nvidia-smi"], capture_output=True, timeout=5)
        if chk.returncode == 0:
            has_nvidia = True
    except Exception as _exc:
        log_suppressed("agents.merger.encoder._auto_setup_nvenc_linux", _exc)
    if not has_nvidia:
        try:
            import torch
            has_nvidia = torch.cuda.is_available()
        except Exception as _exc:
            log_suppressed("agents.merger.encoder._auto_setup_nvenc_linux", _exc)

    if not has_nvidia:
        return None

    # Configure LD_LIBRARY_PATH for NVIDIA CUDA and NVENC driver libraries on Linux
    _ensure_linux_cuda_ld_path()

    target_dir = os.path.abspath(os.path.join("temp", "ffmpeg_nvenc"))
    target = os.path.join(target_dir, "ffmpeg")
    ffprobe_target = os.path.join(target_dir, "ffprobe")
    if os.path.exists(target) and os.path.getsize(target) > 10000000:
        try:
            chk = subprocess.run([target, "-y", "-f", "lavfi", "-i", "nullsrc=s=64x64:d=0.1", "-c:v", "h264_nvenc", "-f", "null", "-"], capture_output=True, timeout=15)
            if chk.returncode == 0:
                os.environ["IMAGEIO_FFMPEG_EXE"] = target
                global _DETECTED_ENCODER
                _DETECTED_ENCODER = None
                return target
        except Exception as _exc:
            log_suppressed("agents.merger.encoder._auto_setup_nvenc_linux", _exc)

    print("[*] Hardware Detection: NVIDIA GPU detected! Auto-fetching static NVENC FFmpeg build...")
    urls = [
        "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz",
        "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-n7.1-latest-linux64-gpl.tar.xz",
    ]
    try:
        build_dir = os.path.join("temp", "ffmpeg_nvenc_build")
        os.makedirs(build_dir, exist_ok=True)
        downloaded = False
        archive_path = os.path.join(build_dir, "ffmpeg.tar.xz")
        for url in urls:
            try:
                res = subprocess.run(["curl", "-L", "-f", "-s", "-A", "Mozilla/5.0", url, "-o", archive_path], timeout=90)
                if res.returncode == 0 and os.path.exists(archive_path) and os.path.getsize(archive_path) > 10000000:
                    downloaded = True
                    break
            except Exception:
                continue

        if not downloaded:
            print("[WARN] Could not download static NVENC FFmpeg build from primary or fallback URLs.")
            return None

        os.makedirs(target_dir, exist_ok=True)
        extracted = set()
        with tarfile.open(archive_path, "r:xz") as archive:
            for member in archive.getmembers():
                name = os.path.basename(member.name)
                if name not in {"ffmpeg", "ffprobe"} or not member.isfile():
                    continue
                destination = target if name == "ffmpeg" else ffprobe_target
                source = archive.extractfile(member)
                if source is None:
                    continue
                with open(destination, "wb") as output:
                    shutil.copyfileobj(source, output)
                os.chmod(destination, 0o755)
                extracted.add(name)
        if "ffmpeg" not in extracted:
            raise RuntimeError("Downloaded archive did not contain ffmpeg")
        try:
            os.remove(archive_path)
        except OSError:
            pass
        os.environ["IMAGEIO_FFMPEG_EXE"] = target
        
        chk = subprocess.run([target, "-y", "-f", "lavfi", "-i", "nullsrc=s=64x64:d=0.1", "-c:v", "h264_nvenc", "-f", "null", "-"], capture_output=True, timeout=15)
        if chk.returncode == 0:
            print(f"🚀 [OK] NVIDIA NVENC GPU Encoder ready and active ({target})!")
            _DETECTED_ENCODER = None
            return target
    except Exception as e:
        print(f"[!] Auto NVENC FFmpeg install notice: {e}")
    return None

def _get_ffmpeg_bin() -> str:
    import shutil
    import subprocess
    # Ensure Linux driver libraries are visible to dynamic linker before any test
    _ensure_linux_cuda_ld_path()

    # Search all candidate ffmpeg paths
    search_paths = [
        "/usr/local/bin/ffmpeg",
        shutil.which("ffmpeg"),
        os.environ.get("IMAGEIO_FFMPEG_EXE"),
        "/usr/bin/ffmpeg",
    ]
    existing = [p for p in search_paths if p and os.path.exists(p)]
    
    # Priority 1: Pick any binary that actively supports NVIDIA NVENC
    for p in existing:
        try:
            res = subprocess.run([p, "-y", "-f", "lavfi", "-i", "nullsrc=s=64x64:d=0.1", "-c:v", "h264_nvenc", "-f", "null", "-"], capture_output=True, timeout=15)
            if res.returncode == 0:
                os.environ["IMAGEIO_FFMPEG_EXE"] = p
                return p
        except Exception as _exc:
            log_suppressed("agents.merger.encoder._get_ffmpeg_bin", _exc)

    # Priority 1.5: If on Linux with NVIDIA GPU, auto-setup NVENC build if not found
    nvenc_p = _auto_setup_nvenc_linux()
    if nvenc_p:
        return nvenc_p

    # Priority 2: Pick any binary that supports Intel QSV
    for p in existing:
        try:
            res = subprocess.run([p, "-y", "-f", "lavfi", "-i", "nullsrc=s=64x64:d=0.1", "-c:v", "h264_qsv", "-f", "null", "-"], capture_output=True, timeout=15)
            if res.returncode == 0:
                os.environ["IMAGEIO_FFMPEG_EXE"] = p
                return p
        except Exception as _exc:
            log_suppressed("agents.merger.encoder._get_ffmpeg_bin", _exc)

    if existing:
        return existing[0]

    try:
        from imageio_ffmpeg import get_ffmpeg_exe
        ffmpeg_bin = get_ffmpeg_exe()
        os.environ["IMAGEIO_FFMPEG_EXE"] = ffmpeg_bin
        return ffmpeg_bin
    except Exception:
        return "ffmpeg"

def detect_hardware_encoder() -> dict:
    """Detects available hardware video encoders (NVIDIA NVENC, Intel QSV, AMD AMF) or CPU libx264."""
    global _DETECTED_ENCODER
    if _DETECTED_ENCODER is not None and _DETECTED_ENCODER.get("type") == "gpu":
        return _DETECTED_ENCODER

    import subprocess
    ffmpeg_bin = _get_ffmpeg_bin()

    candidates = [
        {"codec": "h264_nvenc", "label": "NVIDIA GPU (NVENC)", "type": "gpu", "preset": "p4"},
        {"codec": "h264_qsv", "label": "Intel QuickSync (QSV)", "type": "gpu", "preset": "faster"},
        {"codec": "h264_amf", "label": "AMD Radeon (AMF)", "type": "gpu", "preset": "speed"},
        {"codec": "libx264", "label": "CPU Multi-Core (libx264)", "type": "cpu", "preset": "veryfast"},
    ]

    chosen = candidates[-1]
    for c in candidates[:-1]:
        cmd = [ffmpeg_bin, "-y", "-f", "lavfi", "-i", "nullsrc=s=64x64:d=0.1", "-c:v", c["codec"], "-f", "null", "-"]
        try:
            res = subprocess.run(cmd, capture_output=True, timeout=15)
            if res.returncode == 0:
                chosen = c
                break
        except Exception:
            continue

    _DETECTED_ENCODER = chosen
    return chosen
