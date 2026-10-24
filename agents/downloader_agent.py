from brain.diagnostics import log_suppressed
import os
import re
import time

class DownloaderAgent:
    def __init__(self, output_dir: str = "movies"):
        self.output_dir = output_dir
        os.makedirs(self.output_dir, exist_ok=True)

    @staticmethod
    def is_url(input_string: str) -> bool:
        """Check if the provided input string is a valid URL."""
        url_pattern = re.compile(
            r'^(https?://|www\.)[a-zA-Z0-9\-\.]+\.[a-zA-Z]{2,}(/.*)?$'
            r'|^https?://.+',  # Fix: also match short URLs like youtu.be/... and any https:// link
            re.IGNORECASE
        )
        return bool(url_pattern.match(input_string.strip()))

    @staticmethod
    def find_cookies_file(extra_dirs=()):
        """
        Returns the first usable cookies.txt (Netscape format) from every location the studio knows
        (env var, project root, assets/, Kaggle, Colab, Google Drive) or None.
        Shared by all three engines so a cookie file uploaded once in the dashboard works everywhere.

        YouTube anti-bot အတွက် cookies.txt ကို ရှာဖွေပေးသော shared function (Engine ၃ ခုလုံး သုံးသည်)။
        """
        import glob
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        candidates = [
            os.environ.get('MOVIE_COOKIES_PATH', ''),
            os.path.join(project_root, 'cookies.txt'),
            os.path.join(project_root, 'assets', 'cookies.txt'),
            os.path.abspath(os.path.join(os.path.dirname(project_root), 'Movie_Translate_Private', 'cookies.txt')),
            'cookies.txt',
            os.path.join('assets', 'cookies.txt'),
            '/kaggle/working/cookies.txt',
            '/kaggle/working/pai-ai-movie-studio/cookies.txt',
            '/content/pai-ai-movie-studio/cookies.txt',
            '/content/cookies.txt',
            '/content/drive/MyDrive/MovieRecapOutputs/cookies.txt',
        ]
        candidates += [os.path.join(str(dir_), 'cookies.txt') for dir_ in extra_dirs]
        candidates += glob.glob('/kaggle/input/**/cookies*.txt', recursive=True)
        for path in candidates:
            if path and os.path.exists(path) and os.path.getsize(path) > 10:
                return path
        return None

    @staticmethod
    def _clean_url(url: str) -> str:
        """Strips tracking parameters (?si=..., &feature=...) and formats clean canonical YouTube URL."""
        url = str(url or "").strip()
        if "youtube.com" in url or "youtu.be" in url:
            m = re.search(r'(?:v=|youtu\.be\/|\/embed\/|\/v\/)([0-9A-Za-z_-]{11})', url)
            if m:
                return f"https://www.youtube.com/watch?v={m.group(1)}"
        return url

    def download_video(
        self,
        url: str,
        resolution: str = "1080p",
        custom_filename: str = None,
        progress_callback = None,
    ) -> str:
        """Download video from URL using yt-dlp and return the local file path."""
        url = self._clean_url(url)
        print(f"[*] DownloaderAgent: URL detected -> {url}", flush=True)
        print(f"[*] DownloaderAgent: Downloading video into '{self.output_dir}/' (Resolution: {resolution})...", flush=True)
        
        try:
            import yt_dlp
        except ImportError:
            raise ImportError("yt-dlp is not installed. Please run: pip install yt-dlp")

        import shutil
        ffmpeg_bin = shutil.which("ffmpeg") or os.environ.get("IMAGEIO_FFMPEG_EXE")
        if not ffmpeg_bin:
            try:
                from imageio_ffmpeg import get_ffmpeg_exe
                ffmpeg_bin = get_ffmpeg_exe()
            except Exception:
                ffmpeg_bin = None

        # Check for optional cookies.txt (shared discovery used by every engine)
        active_cookie = self.find_cookies_file(extra_dirs=(self.output_dir,))
        if active_cookie:
            print(f"[*] DownloaderAgent: Using cookies authentication from -> {active_cookie} ({os.path.getsize(active_cookie)} bytes)", flush=True)
        else:
            print("[!] DownloaderAgent: No cookies.txt found in search paths. Using anonymous datacenter bypass mode...", flush=True)

        # Configure video resolution format
        res_key = str(resolution or "1080p").strip().lower()
        if res_key in ["480p", "480"]:
            fmt_spec = 'bestvideo[height<=480]+bestaudio/best[height<=480]/best'
        elif res_key in ["720p", "720"]:
            fmt_spec = 'bestvideo[height<=720]+bestaudio/best[height<=720]/best'
        elif res_key in ["best", "max", "4k", "2160p", "1440p"]:
            fmt_spec = 'bestvideo+bestaudio/best'
        else:
            fmt_spec = 'bestvideo[height<=1080]+bestaudio/best[height<=1080]/best'

        if custom_filename:
            safe_name = re.sub(r'[^\w\-_. ]', '_', str(custom_filename)).strip('. ')
            outtmpl = os.path.join(self.output_dir, f"{safe_name}.%(ext)s")
        else:
            outtmpl = os.path.join(self.output_dir, '%(title)s.%(ext)s')

        # Configure yt-dlp options
        ydl_opts = {
            'format': fmt_spec,
            'outtmpl': outtmpl,
            'restrictfilenames': True,  # Ensure clean filenames without weird symbols
            'noplaylist': True,
            'quiet': False,
            'no_warnings': True,
            'retries': 20,              # Retry up to 20 times on network timeout/errors
            'fragment_retries': 20,     # Retry fragmented streams up to 20 times
            'sleep_interval': 2,
            'socket_timeout': 60,       # Increase socket timeout to 60 seconds
            'http_chunk_size': 10485760,# 10MB chunk size to prevent throttling freeze
        }
        if active_cookie:
            ydl_opts['cookiefile'] = active_cookie
        if ffmpeg_bin:
            ydl_opts['ffmpeg_location'] = ffmpeg_bin

        last_progress_time = [0]
        def _dl_progress(d):
            if progress_callback and callable(progress_callback):
                try:
                    progress_callback(d)
                except Exception as _exc:
                    log_suppressed("agents.downloader_agent._dl_progress", _exc)
            if d.get('status') == 'downloading':
                now = time.time()
                if now - last_progress_time[0] >= 1.5:
                    last_progress_time[0] = now
                    pct = d.get('_percent_str', '').strip()
                    speed = d.get('_speed_str', '').strip()
                    eta = d.get('_eta_str', '').strip()
                    print(f"[*] Downloading: {pct} at {speed} (ETA: {eta})", flush=True)
            elif d.get('status') == 'finished':
                print("[*] Download completed. Processing video streams...", flush=True)

        ydl_opts['progress_hooks'] = [_dl_progress]

        import shutil
        node_bin = shutil.which("node") or shutil.which("nodejs") or shutil.which("deno")
        if node_bin:
            ydl_opts['js_runtimes'] = {'node': {'path': node_bin} if 'node' in node_bin else {'deno': {'path': node_bin}}}

        ydl_opts['http_headers'] = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9',
        }

        max_attempts = 4
        last_error = None
        filename = None
        for attempt in range(1, max_attempts + 1):
            try:
                current_opts = dict(ydl_opts)
                if attempt == 1:
                    if active_cookie:
                        # Attempt 1 (With Cookies): Web/MWeb/TV client natively supports cookies
                        print(f"[*] DownloaderAgent: Fetching with authenticated Web/TV client using {os.path.basename(active_cookie)} (Attempt 1/{max_attempts})...", flush=True)
                        current_opts['extractor_args'] = {'youtube': {'player_client': ['web', 'mweb', 'tv']}}
                        current_opts['cookiefile'] = active_cookie
                    else:
                        # Attempt 1 (No Cookies): VisionOS HLS client (bypasses datacenter IP bot checks)
                        print(f"[*] DownloaderAgent: Fetching 1080p stream with Apple VisionOS client (Attempt 1/{max_attempts})...", flush=True)
                        current_opts['extractor_args'] = {'youtube': {'player_client': ['visionos']}}
                elif attempt == 2:
                    # Attempt 2: Anonymous VisionOS fallback (drops cookies if they were expired or rejected)
                    print(f"[*] DownloaderAgent: Retrying with Pure VisionOS anonymous client (Attempt 2/{max_attempts})...", flush=True)
                    current_opts.pop('cookiefile', None)
                    current_opts['extractor_args'] = {'youtube': {'player_client': ['visionos']}}
                elif attempt == 3:
                    # Attempt 3: Android mobile client without cookies (avoids cookie-mismatch check)
                    print(f"[*] DownloaderAgent: Retrying with Android mobile client (Attempt 3/{max_attempts})...", flush=True)
                    current_opts.pop('cookiefile', None)
                    current_opts['extractor_args'] = {'youtube': {'player_client': ['android']}}
                    current_opts['format'] = 'bestvideo[height<=1080]+bestaudio/best[height<=1080]/best/18/22'
                elif attempt == 4:
                    # Attempt 4: iOS + Android progressive stream fallback
                    print(f"[*] DownloaderAgent: Retrying with iOS/Android direct progressive fallback (Attempt 4/{max_attempts})...", flush=True)
                    current_opts.pop('cookiefile', None)
                    current_opts['extractor_args'] = {'youtube': {'player_client': ['ios', 'android']}}
                    current_opts['format'] = 'best/18/22/bestvideo+bestaudio'

                with yt_dlp.YoutubeDL(current_opts) as ydl:
                    print(f"[*] DownloaderAgent: Fetching video stream (Attempt {attempt}/{max_attempts})...", flush=True)
                    info_dict = ydl.extract_info(url, download=True)
                    if not info_dict:
                        raise RuntimeError(f"yt-dlp could not extract stream metadata for URL: {url}")
                    filename = ydl.prepare_filename(info_dict)
                    
                    # If extension changed during merging, ensure we point to the existing file
                    if filename and not os.path.exists(filename):
                        base, _ = os.path.splitext(filename)
                        for ext in ['.mp4', '.mkv', '.webm', '.m4v', '.mov', '.avi', '.ts']:
                            if os.path.exists(base + ext):
                                filename = base + ext
                                break
                        else:
                            parent_dir = os.path.dirname(filename) or self.output_dir
                            base_name = os.path.splitext(os.path.basename(filename))[0]
                            if os.path.exists(parent_dir):
                                for cand in os.listdir(parent_dir):
                                    cand_path = os.path.join(parent_dir, cand)
                                    if cand.startswith(base_name) and not cand.endswith(('.part', '.ytdl', '.tmp')):
                                        if os.path.isfile(cand_path) and os.path.getsize(cand_path) > 1000:
                                            filename = cand_path
                                            break
                break # Download succeeded
            except Exception as e:
                last_error = e
                print(f"[!] DownloaderAgent Attempt {attempt} failed: {e}")
                # Clean up partial download files
                fn = locals().get('filename')
                if fn:
                    for ext in ['.part', '.ytdl', '.tmp']:
                        partial = fn + ext
                        if os.path.exists(partial):
                            try:
                                os.remove(partial)
                                print(f"[*] Downloader: Removed partial file {os.path.basename(partial)}")
                            except Exception as _exc:
                                log_suppressed("agents.downloader_agent.download_video", _exc)
                if attempt == max_attempts:
                    if "Sign in to confirm" in str(e) or "bot" in str(e).lower():
                        raise RuntimeError(
                            f"\n❌ [YouTube Anti-Bot Challenge]\n"
                            f"YouTube has blocked unauthenticated downloads from this cloud server IP address.\n"
                            f"💡 SOLUTIONS (အလွယ်ဆုံး ဖြေရှင်းနည်း ၂ သွယ်):\n"
                            f"1. Cookies အသုံးပြုခြင်း: Chrome Extension 'Get cookies.txt LOCALLY' ဖြင့် cookies.txt ထုတ်ယူပြီး Web UI သို့မဟုတ် Google Drive (/content/drive/MyDrive/MovieRecapOutputs/cookies.txt) တွင် တင်ပေးပါ။\n"
                            f"2. ဖိုင်တိုက်ရိုက်တင်ခြင်း: ဗီဒီယိုကို မိမိစက်ထဲ ဒေါင်းလုဒ်ဆွဲပြီး Web UI ပေါ်ရှိ 'Upload Movie File' မှတစ်ဆင့် တိုက်ရိုက် တင်နိုင်ပါသည် ခင်ဗျာ။\n"
                            f"Original Error: {e}"
                        )
                    raise e
                print("[*] Retrying in 5 seconds with fallback client format...")
                time.sleep(5)

        # Verify downloaded file actually exists before returning
        if not filename or not os.path.exists(filename):
            raise FileNotFoundError(
                f"[ERROR] DownloaderAgent: Download seemed to succeed but file not found: {filename}\n"
                f"Possible cause: yt-dlp merged into a different extension. Check '{self.output_dir}/' manually."
            )
        print(f"[OK] DownloaderAgent: Video downloaded successfully -> {filename}")
        return filename
