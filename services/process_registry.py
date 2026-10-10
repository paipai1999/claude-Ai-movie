"""
Per-Job Child Process Registry (Job အလိုက် Process ခြေရာခံစနစ်)
===============================================================
Every external process the pipelines start (ffmpeg, demucs, whisper helper, yt-dlp ...) is
recorded under the job that started it. "Stop" can then terminate exactly that job's process
tree instead of sweeping every ffmpeg child of the server.

How it works: `install()` wraps `subprocess.Popen.__init__` once. A resolver supplied by the web
server tells which job the current thread belongs to; the new PID is stored together with the
process creation time, so a recycled PID of an unrelated program is never killed by mistake.

Job တစ်ခု စတင်သော ffmpeg / demucs / whisper စသည့် process များကို ထို job နာမည်အောက်တွင် မှတ်သားထားပြီး
Stop နှိပ်လျှင် ထို job ၏ process tree ကိုသာ ရပ်တန့်စေသည်။
"""

from brain.diagnostics import log_suppressed
import subprocess
import threading
from typing import Callable, Dict, Optional, Set, Tuple

import psutil

# job_id -> {(pid, process_create_time)}
_registry: Dict[str, Set[Tuple[int, float]]] = {}
_lock = threading.Lock()
_installed = False
_resolver: Callable[[], Optional[str]] = lambda: None

# Safety valve for jobs that spawn very many short-lived processes (one ffprobe per frame...)
_PRUNE_THRESHOLD = 256


def _create_time(pid: int) -> Optional[float]:
    try:
        return psutil.Process(pid).create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


def register(job_id: str, pid: int) -> None:
    """Records `pid` as belonging to `job_id` (no-op when the process already vanished)."""
    ctime = _create_time(pid)
    if ctime is None:
        return
    with _lock:
        entries = _registry.setdefault(job_id, set())
        entries.add((pid, ctime))
        if len(entries) > _PRUNE_THRESHOLD:
            _registry[job_id] = {(p, c) for (p, c) in entries if _create_time(p) == c}


def install(resolver: Callable[[], Optional[str]]) -> None:
    """Starts tracking new subprocesses. `resolver()` must return the current job id or None."""
    global _installed, _resolver
    _resolver = resolver
    if _installed:
        return
    original_init = subprocess.Popen.__init__

    def tracked_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        try:
            job_id = _resolver()
            if job_id:
                register(job_id, self.pid)
        except Exception as _exc:
            log_suppressed("services.process_registry.tracked_init", _exc)  # tracking must never break a subprocess call

    subprocess.Popen.__init__ = tracked_init
    _installed = True


def tracked_pids(job_id: str) -> Set[int]:
    """PIDs registered for `job_id` that are still the same live process."""
    with _lock:
        entries = list(_registry.get(job_id, ()))
    return {pid for pid, ctime in entries if _create_time(pid) == ctime}


def pids_of_other_jobs(job_id: str) -> Set[int]:
    """Live PIDs registered under any job except `job_id` (so a sweep can spare them)."""
    with _lock:
        others = {j: list(e) for j, e in _registry.items() if j != job_id}
    alive: Set[int] = set()
    for entries in others.values():
        alive.update(pid for pid, ctime in entries if _create_time(pid) == ctime)
    return alive


def kill_job_processes(job_id: str) -> int:
    """Terminates the registered processes of `job_id` and all their descendants. Returns kill count."""
    killed = 0
    for pid in tracked_pids(job_id):
        try:
            parent = psutil.Process(pid)
            for child in parent.children(recursive=True):
                try:
                    child.kill()
                    killed += 1
                except (psutil.NoSuchProcess, psutil.AccessDenied) as _exc:
                    log_suppressed("services.process_registry.kill_job_processes", _exc)
            parent.kill()
            killed += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied) as _exc:
            log_suppressed("services.process_registry.kill_job_processes", _exc)
    release(job_id)
    return killed


def release(job_id: str) -> None:
    """Forgets a job's processes (call when the job ends)."""
    with _lock:
        _registry.pop(job_id, None)
