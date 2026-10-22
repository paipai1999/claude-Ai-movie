"""
Pai AI Movie Studio - Modern Web UI & RESTful API Backend
=========================================================
Burmese:
  ဤ module သည် AI Movie Recap စနစ်၏ Web Dashboard နှင့် RESTful API ဝန်ဆောင်မှုများကို
  FastAPI ဖြင့် တည်ဆောက်ထားသော ဗဟိုအချက်အချာ ဖြစ်ပါသည်။
  အဓိက အစိတ်အပိုင်းများ:
    - 3-Engine Production Pipeline (Full Movie Recap, Subtitle Engine, Anti-Copyright Hardsub)
    - Sequential FIFO Queue Worker (အလုပ်များ တန်းစီ၍ တစ်ခုပြီးမှ တစ်ခု အလိုအလျောက် ဆောင်ရွက်ခြင်း)
    - Zero-Latency SSE Realtime Streaming (Console log များကို Web UI သို့ အချိန်နှင့်တပြေးညီ ပို့ဆောင်ခြင်း)
    - Multi-Channel Security & Authentication (Token, Bearer Header, Cookie)
    - System Diagnostics & Hardware Acceleration Health Checks (QSV, NVENC, AMF, Gemini API, Disk)

English:
  FastAPI-based dashboard backend providing RESTful endpoints, asynchronous queue dispatching,
  thread-safe execution state management, and real-time Server-Sent Events (SSE) log streaming
  for automated multi-lingual movie video generation.
"""

from brain.diagnostics import log_suppressed
import os
import sys
import re
import json
import uuid
import time
import threading
import shutil
import posixpath
import asyncio
import zipfile
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, parse_qs
from typing import Optional, List, Dict, Any, Callable

from fastapi import FastAPI, Request, UploadFile, File, HTTPException, Query
from fastapi.responses import HTMLResponse, FileResponse, PlainTextResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from sse_starlette.sse import EventSourceResponse

from agents.downloader_agent import DownloaderAgent
from agents.video_merger_agent import detect_hardware_encoder
from brain.sqlite_store import (
    delete_movie_state,
    list_movie_states,
    clean_stale_running_jobs,
    create_job,
    update_job,
    store_queued_job,
    load_queued_jobs,
)
from brain import config as cfg
from brain.config import SUBTITLE_PRESETS
from core.paths import resolve_inside
from main import check_dependencies
from services import security, process_registry


# =============================================================================
# SECTION 1: SYSTEM INITIALIZATION & HARDWARE ACCELERATION
# (စနစ် စတင်လည်ပတ်ခြင်းနှင့် Hardware အရှိန်မြှင့်စနစ် စစ်ဆေးခြင်း)
# =============================================================================

# The dashboard runs jobs on a server: never pop a media player on that machine.
# (CLI runs keep the "open finished video" convenience.)
os.environ.setdefault("PAI_DISABLE_AUTO_OPEN", "1")

# Verify system dependencies (FFmpeg, FFprobe) and detect GPU video encoder.
# check_dependencies() ends with sys.exit() when FFmpeg is missing – catch SystemExit as well,
# otherwise the dashboard (and its health-check page) could never start to report the problem.
try:
    check_dependencies()
    _enc = detect_hardware_encoder()
    print(f"[WebUI Hardware Acceleration] Active Video Encoder: {_enc.get('label', 'Default')} [{_enc.get('codec', 'libx264')}]")
except (Exception, SystemExit) as e:
    print(f"[WARN] check_dependencies failed: {e}")

# Recover from unexpected server crash: mark previously uncompleted running jobs as error/stale
try:
    stale_count = clean_stale_running_jobs()
    if stale_count > 0:
        print(f"[*] WebUI: Reset {stale_count} stale running job(s) from previous session.")
except Exception as e:
    print(f"[WARN] clean_stale_running_jobs notice: {e}")


# =============================================================================
# SECTION 2: FASTAPI APP & AUTHENTICATION MIDDLEWARE
# (FastAPI အက်ပ်နှင့် လုံခြုံရေး စိစစ်မှုစနစ်)
# =============================================================================

from contextlib import asynccontextmanager


@asynccontextmanager
async def _lifespan(_app):
    """Server startup hook: put jobs that were still queued before a restart back in line."""
    if os.getenv("PAI_RESTORE_QUEUE", "1") != "0":
        try:
            _restore_queued_jobs()
        except Exception as e:
            print(f"[WARN] Could not restore queued jobs: {e}")
    yield


app = FastAPI(title="AI Movie Recap API", version="2.3.0", lifespan=_lifespan)

# Cross-Origin Resource Sharing (CORS) setup.
# Secure default: the dashboard is same-origin, so NO foreign website may call the API.
# Only the local dashboard origins are listed; extra origins must be opted in explicitly
# through WEB_UI_ALLOWED_ORIGINS="https://a.example,https://b.example" ("*" is still honoured
# for people who really want it, but is never the default).
# CORS ပုံသေ: အခြား website များ API ကို မခေါ်နိုင်ရန် localhost origin များကိုသာ ခွင့်ပြုထားသည်။
_LOCAL_DEFAULT_ORIGINS = [
    "http://127.0.0.1:5000", "http://localhost:5000",
    "http://127.0.0.1:3000", "http://localhost:3000",
]
_configured_origins = security.parse_allowed_origins(os.getenv("WEB_UI_ALLOWED_ORIGINS", ""))
if _configured_origins == ["*"]:
    _allowed_origins = ["*"]
    _allow_credentials = False  # W3C: credentials cannot be combined with a wildcard origin
elif _configured_origins:
    _allowed_origins = _configured_origins
    _allow_credentials = True
else:
    _allowed_origins = list(_LOCAL_DEFAULT_ORIGINS)
    _allow_credentials = False

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=_allow_credentials,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "x-auth-token"],
)


def _is_authenticated(request: Request) -> bool:
    """
    Verifies client authentication credentials against WEB_UI_PASSWORD or WEB_UI_TOKEN.
    အသုံးပြုသူ၏ လျှို့ဝှက်ကုဒ် သို့မဟုတ် Token ကို စစ်ဆေးပေးသည်။
    Supports 4 verification channels:
      1. HTTP Authorization: Bearer <token>
      2. HTTP Header: x-auth-token: <token>
      3. URL Query Parameter: ?token=<token>
      4. HTTP Secure Cookie: web_ui_token=<token>
    """
    auth_secret = security.get_auth_secret()
    if not auth_secret:
        return True  # Authentication disabled if no environment secret is configured

    # 1. Bearer token in Authorization header
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer ") and security.secrets_match(auth_header[7:], auth_secret):
        return True

    # 2. Custom header (x-auth-token)
    if security.secrets_match(request.headers.get("x-auth-token", ""), auth_secret):
        return True

    # 3. Query parameter for direct browser links or testing
    if security.secrets_match(_query_token(request), auth_secret):
        return True

    # 4. Session cookie (stores a digest of the secret, never the secret itself)
    if security.secrets_match(request.cookies.get("web_ui_token", ""), security.session_cookie_value(auth_secret)):
        return True

    return False


def _query_token(request: Request) -> str:
    """Returns the token passed as ?token= / ?auth= / ?password= (empty string when absent)."""
    return (
        request.query_params.get("token")
        or request.query_params.get("auth")
        or request.query_params.get("password")
        or ""
    )


def _login_page(error: str = "") -> str:
    """Minimal password page. The form POSTs to /api/login so the secret never lands in a URL."""
    msg = f"<p style='color:#f85149;font-size:14px;'>{error}</p>" if error else ""
    return """<!DOCTYPE html><html><head><title>Dashboard Login</title><meta name='viewport' content='width=device-width, initial-scale=1'>
<style>body{font-family:-apple-system,sans-serif;background:#0d1117;color:#c9d1d9;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;}
.box{background:#161b22;padding:32px;border-radius:12px;border:1px solid #30363d;max-width:360px;width:90%;text-align:center;}
input{width:100%;box-sizing:border-box;padding:12px;margin:16px 0;background:#0d1117;border:1px solid #30363d;color:#fff;border-radius:6px;font-size:15px;}
button{width:100%;padding:12px;background:#238636;color:#fff;border:none;border-radius:6px;cursor:pointer;font-weight:bold;font-size:15px;}
button:hover{background:#2ea043;}
</style></head><body><div class='box'><h2>Login Required</h2><p style='color:#8b949e;font-size:14px;'>This Movie Recap Web UI is password-protected.</p>""" + msg + """
<form method='POST' action='/api/login'><input type='password' name='token' placeholder='Enter Password / Token' required autofocus/><button type='submit'>Access Dashboard</button></form>
</div></body></html>"""


def _set_session_cookie(response, request: Request, secret: str) -> None:
    """Persists the browser session (digest value, HttpOnly, SameSite, Secure over HTTPS)."""
    is_https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").lower() == "https"
    response.set_cookie(
        "web_ui_token",
        security.session_cookie_value(secret),
        max_age=86400 * 7,
        httponly=True,
        samesite="lax",
        secure=is_https,
    )


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """
    Request gate for the whole dashboard:
      1. Anonymous mode (no secret configured): rejects cross-site state-changing calls and
         DNS-rebinding Host headers, so a random website cannot drive the local API.
      2. Password mode: rejects every request that is not authenticated.

    1) Password မသတ်မှတ်ထားလျှင် အခြား website မှ ခေါ်သော POST/DELETE များကို ပိတ်ပင်သည်။
    2) Password သတ်မှတ်ထားလျှင် ခွင့်ပြုချက်မရှိသော Request အားလုံးကို ပိတ်ပင်သည်။
    """
    auth_secret = security.get_auth_secret()
    path = request.url.path

    if not auth_secret:
        host_header = request.headers.get("host", "")
        bind_host = getattr(app.state, "bind_host", None)
        if not security.host_is_trusted(host_header, bind_host):
            return JSONResponse(status_code=403, content={"status": "error", "detail": "Untrusted Host header"})
        if request.method not in security.SAFE_METHODS:
            if not security.origin_is_allowed(request.headers.get("origin"), host_header, _configured_origins):
                return JSONResponse(status_code=403, content={"status": "error", "detail": "Cross-site request blocked"})
    else:
        exempt = path in ["/api/login", "/api/auth/status", "/favicon.ico"] or path.startswith("/static")
        if not exempt and not _is_authenticated(request):
            if path.startswith("/api/"):
                return JSONResponse(status_code=401, content={"status": "error", "detail": "Unauthorized: Password or Token required"})
            return HTMLResponse(content=_login_page(), status_code=401)

    response = await call_next(request)
    # Persist a session cookie after a successful ?token= visit
    if auth_secret and security.secrets_match(_query_token(request), auth_secret):
        _set_session_cookie(response, request, auth_secret)
    return response


# Brute-force throttle for /api/login: {client_ip: [failure timestamps]}
_login_failures: Dict[str, List[float]] = {}
_LOGIN_MAX_FAILURES = 10
_LOGIN_WINDOW_SEC = 60


@app.post("/api/login")
async def login(request: Request):
    """
    Password login endpoint used by the login page (form POST) or scripts (JSON {"token": ...}).
    Sets the session cookie and redirects (form) / returns JSON (script).
    """
    secret = security.get_auth_secret()
    raw = (await request.body()).decode("utf-8", errors="replace")
    wants_json = "application/json" in request.headers.get("content-type", "")
    token = ""
    if wants_json:
        try:
            token = str(json.loads(raw).get("token", ""))
        except Exception:
            token = ""
    else:
        token = (parse_qs(raw).get("token") or [""])[0]

    if not secret:  # authentication disabled – nothing to log in to
        return {"success": True, "auth_required": False} if wants_json else RedirectResponse("/", status_code=303)

    client = request.client.host if request.client else "unknown"
    now = time.time()
    recent = [t for t in _login_failures.get(client, []) if now - t < _LOGIN_WINDOW_SEC]
    if len(recent) >= _LOGIN_MAX_FAILURES:
        _login_failures[client] = recent
        return JSONResponse(status_code=429, content={"status": "error", "detail": "Too many attempts. Try again in a minute."})

    if not security.secrets_match(token, secret):
        recent.append(now)
        _login_failures[client] = recent
        if wants_json:
            return JSONResponse(status_code=401, content={"status": "error", "detail": "Invalid password"})
        return HTMLResponse(content=_login_page("Invalid password."), status_code=401)

    _login_failures.pop(client, None)
    response = JSONResponse({"success": True}) if wants_json else RedirectResponse("/", status_code=303)
    _set_session_cookie(response, request, secret)
    return response


@app.get("/api/auth/status")
async def auth_status(request: Request):
    """Tells the frontend whether a password is required and whether this session has it."""
    required = bool(security.get_auth_secret())
    return {"auth_required": required, "authenticated": (not required) or _is_authenticated(request)}


# Jinja2 template renderer for the web dashboard HTML
templates = Jinja2Templates(directory="templates")

# The dashboard's stylesheet and script (static/css, static/js). They hold no secrets, so /static is
# served without login - the auth middleware exempts it so the login page can be styled too.
app.mount("/static", StaticFiles(directory="static", check_dir=False), name="static")


# Upload limits (disk-fill / memory protection). MAX_UPLOAD_MB overrides the 16 GB default.
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_MB", "16384")) * 1024 * 1024
MAX_COOKIE_FILE_BYTES = 1024 * 1024


# =============================================================================
# SECTION 3: SHARED STATE, THREAD SYNCHRONIZATION & QUEUE DISPATCHER
# (မျှဝေသုံး Memory၊ Thread အချင်းချင်း ထိန်းချုပ်မှုနှင့် တန်းစီစနစ် Dispatcher)
# =============================================================================

# In-memory dictionary tracking job state and sequential FIFO queue from services
from services.job_manager import (
    jobs,
    jobs_lock,
    JOB_RETENTION_SECONDS,
    has_running_job,
)
from services.queue_manager import (
    job_queue,
    queue_lock,
)

_dispatcher_thread: Optional[threading.Thread] = None

def _queue_dispatcher():
    """
    Background worker that continuously pulls jobs from the sequential FIFO queue.
    နောက်ကွယ်တွင် အမြဲ စောင့်ကြည့်နေပြီး လက်ရှိအလုပ် ပြီးသည်နှင့် နောက်တစ်ခုကို ချက်ချင်း စတင်ပေးသည်။
    
    Logic:
      1. Checks if any job currently has status == 'running'.
      2. If no jobs are active, pops the oldest job from `job_queue`.
      3. Sets job status to 'running' in memory and SQLite database.
      4. Spawns target worker thread asynchronously.
    """
    while True:
        job_to_run = None
        with jobs_lock:
            running = any(j.get('status') == 'running' for j in jobs.values())
        if not running:
            with queue_lock:
                if job_queue:
                    job_to_run = job_queue.pop(0)
        if job_to_run:
            jid = job_to_run["job_id"]
            should_start = False
            with jobs_lock:
                if jid in jobs and jobs[jid].get("status") == "queued":
                    jobs[jid]["status"] = "running"
                    jobs[jid]["phase"] = "Starting..."
                    should_start = True
                    try:
                        update_job(jid, status="running", phase="Starting...")
                    except Exception as _exc:
                        log_suppressed("web_ui._queue_dispatcher", _exc)
                else:
                    print(f"[*] Queue Dispatcher: Skipping job {jid} (status: {jobs.get(jid, {}).get('status')})")
            if should_start:
                print(f"[*] Queue Dispatcher: Starting next queued job: {jid} ({job_to_run.get('name', 'video')})")
                t = threading.Thread(
                    target=job_to_run["target"],
                    args=job_to_run["args"],
                    daemon=True
                )
                t.start()
        time.sleep(1.0)


def _ensure_queue_dispatcher():
    """
    Ensures the background FIFO queue dispatcher thread is running.
    တန်းစီစနစ် Dispatcher thread လည်ပတ်နေစေရန် စစ်ဆေးစတင်ပေးသည်။
    """
    global _dispatcher_thread
    with queue_lock:
        if _dispatcher_thread is None or not _dispatcher_thread.is_alive():
            _dispatcher_thread = threading.Thread(target=_queue_dispatcher, daemon=True)
            _dispatcher_thread.start()


# Launch queue dispatcher on module load
_ensure_queue_dispatcher()


def _worker_registry() -> Dict[str, Callable]:
    """name -> worker function (resolved lazily because the workers are defined further below)."""
    return {fn.__name__: fn for fn in (pipeline_worker, subtitle_worker, hardsub_worker, batch_worker, downloader_worker,
                                       splitter_worker, shorts_worker)}


def _persist_queued_job(job_entry: Dict[str, Any]) -> None:
    """Stores a freshly queued job so it survives a server restart (best effort)."""
    try:
        target = job_entry.get("target")
        meta = {k: job_entry.get(k) for k in ("name", "source", "language", "tts_engine", "engine_mode", "created_at")}
        if target is None or not store_queued_job(job_entry["job_id"], target.__name__, list(job_entry["args"]), meta):
            print(f"[WARN] Queue persistence skipped for job {job_entry.get('job_id')} (arguments not serialisable).")
    except Exception as e:
        print(f"[WARN] Queue persistence failed for job {job_entry.get('job_id')}: {e}")


def _restore_queued_jobs() -> int:
    """
    Re-creates in-memory jobs + queue entries for rows that were 'queued' when the server stopped.
    Jobs that were already running are marked 'interrupted' by clean_stale_running_jobs() instead
    (re-run them manually; their checkpoints make the resume cheap).

    Server ပြန်စတင်ချိန်တွင် တန်းစီနေဆဲ အလုပ်များကို တန်းစီစာရင်းထဲ ပြန်ထည့်ပေးသည်။
    """
    registry = _worker_registry()
    restored = 0
    for item in load_queued_jobs():
        worker = registry.get(item["worker"])
        job_id = item["job_id"]
        if worker is None:
            continue
        with jobs_lock:
            if job_id in jobs:
                continue
            meta = item.get("meta") or {}
            jobs[job_id] = {
                "status": "queued",
                "phase": "Restored after restart - waiting in queue",
                "buffer": None,
                "created_at": item.get("created_at") or time.time(),
                "name": meta.get("name") or "video",
                "source": meta.get("source") or meta.get("name") or "video",
                "language": meta.get("language") or "burmese",
                "tts_engine": meta.get("tts_engine") or "edge_tts",
            }
        with queue_lock:
            job_queue.append({
                "job_id": job_id,
                "target": worker,
                "args": tuple(item["args"]),
                "name": meta.get("name"),
                "source": meta.get("source"),
                "language": meta.get("language"),
                "tts_engine": meta.get("tts_engine"),
                "engine_mode": meta.get("engine_mode"),
                "created_at": item.get("created_at"),
            })
        restored += 1
    if restored:
        _ensure_queue_dispatcher()
        print(f"[*] WebUI: Restored {restored} queued job(s) from the previous session.")
    return restored


def _cleanup_old_jobs():
    """
    Purges completed, errored, or cancelled jobs older than JOB_RETENTION_SECONDS.
    Also clears associated stdout buffers and SSE subscriber queues to prevent memory leaks.
    
    ပြီးစီး/ရပ်တန့်/မှားယွင်းသွားသော အလုပ်ဟောင်းများကို Memory ရှင်းလင်းပေးပြီး
    Log buffers နှင့် SSE queues များကိုပါ ဖယ်ရှားပေးသည်။
    """
    now = time.time()
    with jobs_lock:
        to_delete = [
            jid for jid, job in list(jobs.items())
            if job.get('status') in ('done', 'error', 'cancelled')
            and now - job.get('created_at', now) > JOB_RETENTION_SECONDS
        ]
        for jid in to_delete:
            jobs.pop(jid, None)
            # Free thread_stdout buffers and subscribers to prevent memory leak
            thread_stdout.buffers.pop(jid, None)
            thread_stdout.subscribers.pop(jid, None)


def _has_running_job() -> bool:
    """
    Returns True if any video generation job is currently executing.
    လက်ရှိ လုပ်ဆောင်ဆဲ အလုပ် ရှိ/မရှိ စစ်ဆေးပေးသည်။
    """
    return has_running_job()


def _safe_child_path(folder_type: str, item_name: str) -> Optional[str]:
    """
    Security check against Path Traversal attacks (e.g., '../../secret_file').
    Ensures candidate path is strictly INSIDE the designated parent folder ('outputs', 'movies', 'temp').
    The folder itself ('.', '', './') is rejected so a delete call can never wipe a whole root folder.

    Path Traversal တိုက်ခိုက်မှုများ မဖြစ်စေရန် ရွေးချယ်ထားသော ဖိုဒါအတွင်း၌သာ ရှိမရှိ စစ်ဆေးပေးပြီး
    ဖိုဒါကိုယ်တိုင် ('.') ကို ပယ်ချသည်။
    """
    if folder_type not in {'outputs', 'movies', 'temp'} or not item_name:
        return None
    return resolve_inside(folder_type, item_name)

from services.job_runtime import (  # noqa: E402  (job execution lives in services/job_runtime.py)
    VIDEO_EXTENSIONS,
    INPUT_EXTENSIONS,
    cancel_events,
    current_job_id,
    thread_stdout,
    _STANDALONE_DOWNLOAD_JOB,
    _is_job_cancelled,
    _set_job_state,
    _project_dir_from_result,
    _resolve_input_source,
    pipeline_worker,
    subtitle_worker,
    hardsub_worker,
    batch_worker,
    downloader_worker,
    splitter_worker,
    shorts_worker,
)
# Names that tests / external callers historically imported from web_ui (kept as a stable surface)
__all__ = [
    "app", "jobs", "jobs_lock", "job_queue", "queue_lock", "INPUT_EXTENSIONS", "VIDEO_EXTENSIONS",
    "_is_job_cancelled", "_set_job_state", "_project_dir_from_result", "_resolve_input_source",
]

from core.options import (  # noqa: E402
    DOWNLOAD, HARDSUB, RECAP, SHORTS, SPLITTER, SUBTITLE,
    describe as describe_options,
    normalize_engine,
    resolve as resolve_options,
)
from services import projects as project_store, studio_api  # noqa: E402
from services.key_store import mask_key, resolve_key_lines  # noqa: E402
from services.job_args import build_worker_args  # noqa: E402
from services.schemas import (  # noqa: E402
    StartRequest,
    BatchStartRequest,
    SubtitleConfigRequest,
    BrandingConfigRequest,
    RenameRequest,
    SaveKeysRequest,
    CookieSaveRequest,
    DownloadVideoRequest,
)

# =============================================================================
# CONSOLE CAPTURE: route stdout / stderr through the per-job log multiplexer
# (Console output ကို job အလိုက် log buffer နှင့် SSE သို့ ပို့ရန် ချိတ်ဆက်ခြင်း)
# =============================================================================
# Ensure UTF-8 console output encoding on Windows
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Intercept global sys.stdout and sys.stderr (thread_stdout wraps the original stream)
sys.stdout = thread_stdout
sys.stderr = thread_stdout


# Gemini key probing (shared by /api/system/health-check and /api/keys/status)
_GEMINI_MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"


def _ping_gemini_key(key: str, timeout: float = 8.0) -> tuple:
    """
    Checks one Gemini API key against the models endpoint and returns (state, http_code).
    The key travels in the `x-goog-api-key` header – it is never placed in the URL, so it cannot
    leak through proxy logs, exception text or browser history.

    API key ကို URL ထဲမထည့်ဘဲ header ဖြင့်သာ ပို့ပြီး အခြေအနေကို စစ်ဆေးပေးသည်။
    """
    key_str = str(key).strip()
    if not key_str:
        return "EMPTY", 0
    req = urllib.request.Request(
        _GEMINI_MODELS_URL,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Recap/2.3", "x-goog-api-key": key_str},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", 0)
            if status == 200:
                return "ONLINE", 200
            return "UNKNOWN", status if isinstance(status, int) else 0
    except urllib.error.HTTPError as e:
        if e.code == 429:
            return "RATE LIMITED (429)", 429
        if e.code in (400, 401, 403):
            return "INVALID KEY", e.code
        return f"HTTP {e.code}", e.code
    except Exception:
        return "UNREACHABLE", 0


def _ping_gemini_keys(keys: List[str], timeout: float = 8.0) -> List[tuple]:
    """Pings several keys in parallel (blocking – call from a worker thread / sync endpoint)."""
    if not keys:
        return []
    with ThreadPoolExecutor(max_workers=min(10, len(keys))) as executor:
        return list(executor.map(lambda k: _ping_gemini_key(k, timeout), keys))


# =============================================================================
# SECTION 7: DASHBOARD & SYSTEM DIAGNOSTICS ENDPOINTS
# (Web Dashboard နှင့် စနစ်ကျန်းမာရေး စစ်ဆေးမှု API များ)
# =============================================================================

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Renders the single-page application (SPA) dashboard."""
    return templates.TemplateResponse(request=request, name="index.html")

@app.get("/api/system/info")
def system_info():
    enc = detect_hardware_encoder()
    with jobs_lock:
        active = _has_running_job()
    return {
        "hardware_encoder": enc,
        "python_version": sys.version.split()[0],
        "platform": sys.platform,
        "active_jobs": active
    }

@app.get("/api/system/update-status")
def get_update_status():
    """Checks git repository status against remote origin."""
    try:
        import subprocess
        # Get current commit hash and branch
        commit_res = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=3)
        commit = commit_res.stdout.strip() if commit_res.returncode == 0 else "unknown"
        
        branch_res = subprocess.run(["git", "branch", "--show-current"], capture_output=True, text=True, timeout=3)
        branch = branch_res.stdout.strip() if branch_res.returncode == 0 else "main"

        # Check rev-list behind origin/main
        behind_res = subprocess.run(["git", "rev-list", "--count", "HEAD..origin/main"], capture_output=True, text=True, timeout=3)
        behind = int(behind_res.stdout.strip()) if (behind_res.returncode == 0 and behind_res.stdout.strip().isdigit()) else 0

        return {
            "status": "ok",
            "branch": branch,
            "commit": commit,
            "updates_available": behind > 0,
            "commits_behind": behind,
            "message": f"{behind} new update(s) available on GitHub." if behind > 0 else "System is up to date with origin/main."
        }
    except Exception as e:
        return {
            "status": "warning",
            "updates_available": False,
            "commits_behind": 0,
            "error": str(e),
            "message": "Git status check unavailable."
        }

@app.get("/api/system/health-check")
async def system_health_check():
    """Performs a comprehensive diagnostic on FFmpeg, GPU Encoder, Gemini Keys, Edge-TTS, and Disk."""
    results = {
        "status": "ok",
        "timestamp": time.time(),
        "checks": {}
    }

    # 1. FFmpeg & Hardware Encoder
    try:
        from agents.video_merger_agent import detect_hardware_encoder, _get_ffmpeg_bin
        ff_bin = _get_ffmpeg_bin()
        enc = detect_hardware_encoder()
        results["checks"]["ffmpeg"] = {
            "status": "ok",
            "installed": bool(ff_bin),
            "binary": os.path.basename(ff_bin) if ff_bin else "not_found",
            "encoder": enc.get("label", "CPU"),
            "codec": enc.get("codec", "libx264"),
            "type": enc.get("type", "cpu"),
            "is_gpu": enc.get("type") == "gpu",
            "details": f"{enc.get('label', 'CPU')} ({enc.get('codec', 'libx264')})"
        }
    except Exception as e:
        results["checks"]["ffmpeg"] = {"status": "error", "installed": False, "healthy": False, "error": str(e), "details": str(e)}
        results["status"] = "warning"

    # 2. Gemini API Keys Health (probed in worker threads so the event loop / SSE streams never stall)
    try:
        c = cfg.load_config()
        keys = list(c.get("gemini", {}).get("api_keys", []) or [])   # copy: never mutate the cached config
        env_k = os.getenv("GEMINI_API_KEYS") or os.getenv("GEMINI_API_KEY")
        if env_k:
            for ek in env_k.replace(";", ",").split(","):
                if ek.strip() and ek.strip() not in keys:
                    keys.append(ek.strip())

        keys = [str(k).strip() for k in keys if str(k).strip()]
        ping_results = await asyncio.get_running_loop().run_in_executor(None, _ping_gemini_keys, keys, 4.0)
        state_map = {"ONLINE": "active", "RATE LIMITED (429)": "rate_limited", "INVALID KEY": "invalid_key",
                     "UNREACHABLE": "connection_error", "UNKNOWN": "unexpected"}

        key_reports = []
        valid_keys = 0
        for k, (state, code) in zip(keys, ping_results):
            masked = (k[:6] + "..." + k[-4:]) if len(k) > 10 else "***"
            status = state_map.get(state, state.lower().replace(" ", "_"))
            if status == "active":
                valid_keys += 1
            report = {"key": masked, "key_preview": masked, "status": status, "code": code}
            if status == "connection_error":
                report["error"] = "unreachable"
            key_reports.append(report)

        gemini_healthy = valid_keys > 0
        gemini_status = "ok" if gemini_healthy else ("rate_limited" if any(r.get("status") == "rate_limited" for r in key_reports) else "warning")
        results["checks"]["gemini"] = {
            "status": gemini_status,
            "healthy": gemini_healthy,
            "total_keys": len(key_reports),
            "valid_keys": valid_keys,
            "results": key_reports,
            "details": key_reports
        }
        if not gemini_healthy:
            results["status"] = "warning"
    except Exception as e:
        results["checks"]["gemini"] = {"status": "error", "healthy": False, "error": str(e), "total_keys": 0, "valid_keys": 0}
        results["status"] = "warning"

    # 3. Edge-TTS Connectivity
    try:
        import edge_tts
        import asyncio
        t0 = time.time()
        async def _check_tts():
            voices = await edge_tts.list_voices()
            return any(v.get("ShortName") == "my-MM-ThihaNeural" for v in voices)
        has_voice = await asyncio.wait_for(_check_tts(), timeout=7.0)
        elapsed = round((time.time() - t0) * 1000, 1)
        results["checks"]["edge_tts"] = {
            "status": "ok",
            "healthy": True,
            "myanmar_voice_available": has_voice,
            "latency_ms": elapsed,
            "details": f"Online (Latency: {elapsed}ms, Myanmar voice ready: {has_voice})"
        }
    except Exception as e:
        results["checks"]["edge_tts"] = {"status": "error", "healthy": False, "error": str(e)[:80], "details": str(e)[:80]}
        results["status"] = "warning"

    # 4. Disk Space Check
    try:
        du = shutil.disk_usage(".")
        free_gb = round(du.free / (1024 ** 3), 2)
        total_gb = round(du.total / (1024 ** 3), 2)
        disk_healthy = free_gb >= 5.0
        results["checks"]["disk"] = {
            "status": "ok" if disk_healthy else "low_disk",
            "healthy": disk_healthy,
            "free_gb": free_gb,
            "total_gb": total_gb,
            "percent_free": round((du.free / du.total) * 100, 1),
            "details": f"{free_gb} GB free of {total_gb} GB ({round((du.free / du.total) * 100, 1)}% available)"
        }
        if not disk_healthy:
            results["status"] = "warning"
    except Exception as e:
        results["checks"]["disk"] = {"status": "error", "healthy": False, "error": str(e), "details": str(e)}

    return results

@app.get("/api/jobs/active")
def get_active_job():
    with jobs_lock:
        for jid, jdata in list(jobs.items()):
            if jdata.get("status") == "running":
                return {
                    "job_id": jid,
                    "name": jdata.get("name", "video"),
                    "source": jdata.get("source") or jdata.get("name", "video"),
                    "language": jdata.get("language", "burmese"),
                    "tts_engine": jdata.get("tts_engine", "edge_tts"),
                    "status": "running",
                    "phase": jdata.get("phase", "Running..."),
                    "created_at": jdata.get("created_at")
                }
    return {"job_id": None, "status": "idle"}

# =============================================================================
# SECTION 8: YOUTUBE ANTI-BOT COOKIES & CREDENTIALS
# (YouTube ဒေါင်းလုဒ်အတွက် Bot အကာအကွယ်ကျော်လွှားမည့် Cookies စီမံခန့်ခွဲမှု)
# =============================================================================

def _get_cookie_paths() -> List[str]:
    """
    Returns candidate file paths where cookies.txt may reside across platforms
    (Local PC, Assets, Kaggle, Google Colab, and Google Drive).
    
    ပလက်ဖောင်း အမျိုးမျိုးတွင် ရှိနိုင်သော cookies.txt တည်နေရာများကို စုစည်းပေးသည်။
    """
    return [
        "cookies.txt",
        os.path.join("assets", "cookies.txt"),
        "/kaggle/working/cookies.txt",
        "/kaggle/working/pai-ai-movie-studio/cookies.txt",
        "/content/pai-ai-movie-studio/cookies.txt",
        "/content/cookies.txt",
        "/content/drive/MyDrive/MovieRecapOutputs/cookies.txt"
    ]


def _save_cookie_content(content_bytes: bytes) -> List[str]:
    """
    Synchronizes Netscape cookie bytes across all relevant runtime directories.
    ပေးပို့လာသော Cookies ဒေတာများကို ပလက်ဖောင်းအလိုက် လိုအပ်သော ဖိုဒါများထဲသို့ သိမ်းဆည်းပေးသည်။
    """
    saved_paths = []
    # 1. Local and assets/ directory
    for p in ["cookies.txt", os.path.join("assets", "cookies.txt")]:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
            with open(p, "wb") as f:
                f.write(content_bytes)
            saved_paths.append(p)
        except Exception as _exc:
            log_suppressed("web_ui._save_cookie_content", _exc)
    # 2. Google Drive for Colab persistent storage
    drive_out = "/content/drive/MyDrive/MovieRecapOutputs"
    if os.path.exists(drive_out):
        try:
            dp = os.path.join(drive_out, "cookies.txt")
            with open(dp, "wb") as df:
                df.write(content_bytes)
            saved_paths.append(dp)
        except Exception as _exc:
            log_suppressed("web_ui._save_cookie_content", _exc)
    # 3. Kaggle working directory
    for kp in ["/kaggle/working/cookies.txt", "/kaggle/working/pai-ai-movie-studio/cookies.txt"]:
        if os.path.exists(os.path.dirname(kp)):
            try:
                with open(kp, "wb") as kf:
                    kf.write(content_bytes)
                saved_paths.append(kp)
            except Exception as _exc:
                log_suppressed("web_ui._save_cookie_content", _exc)
    return saved_paths


@app.get("/api/cookies/status")
def get_cookies_status():
    """
    Checks if cookies.txt is installed, validates format, file size, and YouTube domain presence.
    လက်ရှိ စနစ်တွင် YouTube cookies တပ်ဆင်ထားခြင်း ရှိ/မရှိ စစ်ဆေးပေးသည်။
    """
    candidates = _get_cookie_paths()
    import glob
    for k_match in glob.glob('/kaggle/input/**/cookies*.txt', recursive=True):
        candidates.append(k_match)

    found_path = None
    file_size = 0
    mtime = None
    has_youtube = False
    cookie_count = 0

    for c in candidates:
        if os.path.exists(c) and os.path.getsize(c) > 10:
            found_path = c
            file_size = os.path.getsize(c)
            try:
                mtime = time.ctime(os.path.getmtime(c))
                with open(c, "r", encoding="utf-8", errors="ignore") as f:
                    text = f.read()
                has_youtube = "youtube.com" in text.lower()
                cookie_count = len([line for line in text.splitlines() if line.strip() and not line.startswith("#")])
            except Exception as _exc:
                log_suppressed("web_ui.get_cookies_status", _exc)
            break

    return {
        "installed": bool(found_path),
        "path": found_path,
        "size_bytes": file_size,
        "size_kb": round(file_size / 1024, 2),
        "has_youtube": has_youtube,
        "cookie_count": cookie_count,
        "last_modified": mtime
    }


@app.post("/api/cookies/save")
def save_cookies_text(req: CookieSaveRequest):
    """
    Saves raw Netscape cookies text sent from dashboard UI.
    Web UI မှ တိုက်ရိုက် ရေးသွင်းလိုက်သော Cookies စာသားများကို ဖိုင်အဖြစ် သိမ်းဆည်းပေးသည်။
    """
    content = (req.content or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="Cookies content is empty")
    content_bytes = content.encode("utf-8")
    saved_paths = _save_cookie_content(content_bytes)
    return {
        "success": True,
        "message": f"Cookies saved successfully to {len(saved_paths)} location(s)!",
        "saved_paths": saved_paths,
        "bytes": len(content_bytes)
    }


@app.delete("/api/cookies")
def delete_cookies():
    """
    Removes cookies.txt from all known runtime paths.
    သိမ်းဆည်းထားသော Cookies ဖိုင်များကို စနစ်တစ်ခုလုံးမှ ပယ်ဖျက်ရှင်းလင်းသည်။
    """
    deleted = []
    for c in _get_cookie_paths():
        if os.path.exists(c):
            try:
                os.remove(c)
                deleted.append(c)
            except Exception as _exc:
                log_suppressed("web_ui.delete_cookies", _exc)
    return {"success": True, "deleted": deleted}


# =============================================================================
# SECTION 9: PIPELINE EXECUTION CONTROLLERS (UPLOAD, START & BATCH)
# (ဗီဒီယို ဖိုင်တင်ခြင်း၊ တစ်ခုချင်းစတင်ခြင်းနှင့် Batch ထုတ်လုပ်ခြင်း API များ)
# =============================================================================

@app.post("/api/upload")
async def upload_file(video: UploadFile = File(...)):
    if not video.filename:
        raise HTTPException(status_code=400, detail="No selected file")

    import re as _re
    def _secure_filename(fname: str) -> str:
        """Stdlib-based secure_filename: strips unsafe chars, no werkzeug needed."""
        fname = os.path.basename(fname).strip()
        fname = _re.sub(r'[^\w\-_. ]', '_', fname)
        fname = fname.strip('. ')
        return fname or "upload"

    filename = _secure_filename(video.filename)
    # Support automatic YouTube cookies.txt installation
    if filename.lower() in ["cookies.txt", "cookie.txt"] or filename.lower().endswith(".txt"):
        content = await video.read(MAX_COOKIE_FILE_BYTES + 1)
        if len(content) > MAX_COOKIE_FILE_BYTES:
            raise HTTPException(status_code=413, detail="Cookies file is too large")
        if b"youtube" in content.lower() or b"# Netscape" in content or b"# HTTP Cookie File" in content:
            with open("cookies.txt", "wb") as f:
                f.write(content)
            os.makedirs("assets", exist_ok=True)
            with open(os.path.join("assets", "cookies.txt"), "wb") as f:
                f.write(content)
            drive_out = "/content/drive/MyDrive/MovieRecapOutputs"
            if os.path.exists(drive_out):
                try:
                    with open(os.path.join(drive_out, "cookies.txt"), "wb") as df:
                        df.write(content)
                    print("[*] Upload: cookies.txt permanently saved to Google Drive!")
                except Exception as _exc:
                    log_suppressed("web_ui.upload_file", _exc)
            for kp in ["/kaggle/working/cookies.txt", "/kaggle/working/pai-ai-movie-studio/cookies.txt"]:
                if os.path.exists(os.path.dirname(kp)):
                    try:
                        with open(kp, "wb") as kf:
                            kf.write(content)
                        print(f"[*] Upload: cookies.txt saved to {kp}!")
                    except Exception as _exc:
                        log_suppressed("web_ui.upload_file", _exc)
            print("[*] Upload: cookies.txt installed successfully into root and assets/!")
            return {"success": True, "filename": "cookies.txt", "message": "YouTube cookies installed successfully!"}

    ext = os.path.splitext(filename)[1].lower()
    if ext not in VIDEO_EXTENSIONS:
        raise HTTPException(status_code=400, detail="File is not a supported video format or cookies.txt")
        
    os.makedirs("movies", exist_ok=True)
    save_path = os.path.join("movies", filename)
    written = 0
    try:
        with open(save_path, "wb") as f:
            # BUG-M3 Fix: Stream in 1MB chunks — avoids loading a 4GB file entirely into RAM
            while True:
                chunk = await video.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"Upload exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit (set MAX_UPLOAD_MB to change it)",
                    )
                f.write(chunk)
    except HTTPException:
        try:
            os.remove(save_path)  # never keep a truncated upload
        except OSError:
            pass
        raise


    return {"success": True, "filename": filename}

@app.post("/api/downloader/video")
async def download_video_endpoint(req: DownloadVideoRequest):
    """
    Dedicated video downloader endpoint.
    Downloads a video from YouTube or supported URLs directly into 'movies/' directory.
    """
    url = (req.url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="Video URL is required")
    if not DownloaderAgent.is_url(url):
        raise HTTPException(status_code=400, detail="Invalid video URL. Please provide a valid HTTP/HTTPS video URL.")

    def _do_download():
        # Own owner id: stopping the pipeline must not kill this download's ffmpeg (and vice versa).
        token = current_job_id.set(_STANDALONE_DOWNLOAD_JOB)
        try:
            downloader = DownloaderAgent(output_dir="movies")
            return downloader.download_video(
                url,
                resolution=req.resolution or "1080p",
                custom_filename=req.custom_name,
            )
        finally:
            current_job_id.reset(token)
            process_registry.release(_STANDALONE_DOWNLOAD_JOB)

    try:
        loop = asyncio.get_event_loop()
        file_path = await loop.run_in_executor(None, _do_download)
        filename = os.path.basename(file_path)
        size_bytes = os.path.getsize(file_path) if os.path.exists(file_path) else 0
        return {
            "success": True,
            "filename": filename,
            "path": file_path,
            "size_bytes": size_bytes,
            "message": f"Video downloaded successfully: {filename}"
        }
    except Exception as e:
        err_msg = str(e)
        raise HTTPException(status_code=500, detail=err_msg)

def _worker_for_engine(engine: str):
    """The background worker that executes a given studio engine."""
    return {
        HARDSUB: hardsub_worker,
        SUBTITLE: subtitle_worker,
        DOWNLOAD: downloader_worker,
        SPLITTER: splitter_worker,
        SHORTS: shorts_worker,
    }.get(engine, pipeline_worker)


# Studio endpoints (projects, script editor, quality report, profiles, history) live in services/studio_api.py.
app.include_router(studio_api.router)


@app.post("/api/projects/{name}/rerender")
async def rerender_project(name: str):
    """
    Re-runs a recap project with the settings it was started with. Because the script is cached and the
    voice clips are fingerprinted, only edited lines are re-voiced and the video is re-rendered.
    """
    try:
        project_dir = project_store.resolve_project("outputs", name)
    except project_store.ProjectError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    saved = project_store.load_job_options(project_dir)
    if not saved or not saved.get("input"):
        raise HTTPException(
            status_code=409,
            detail="This project has no saved settings (it was created before re-render existed). Start it "
                   "again from the wizard with the same options and 'Resume' on; your edits are kept.",
        )
    saved["resume"] = True
    return _enqueue_job(RECAP, resolve_options(RECAP, saved))


@app.get("/api/options")
def get_option_model():
    """
    The step-by-step option model (steps, options, defaults per engine). The dashboard wizard,
    the API schemas and the CLI are all generated from this one definition.
    """
    return describe_options()


@app.post("/api/start")
async def start_pipeline(req: StartRequest):
    input_source = req.input
    if not input_source:
        raise HTTPException(status_code=400, detail="No input provided")

    # Every default is applied in ONE place (core.options.resolve) for the chosen engine.
    engine = normalize_engine(req.engine_mode)
    return _enqueue_job(engine, resolve_options(engine, req.model_dump()))


def _enqueue_job(engine: str, opts: Dict[str, Any]) -> Dict[str, Any]:
    """
    Registers a job for the resolved options and either starts it or puts it in the FIFO queue.
    Shared by ``/api/start`` and the project "re-render" action.
    """
    input_source = opts["input"]
    language = opts["language"]
    tts_engine = opts["tts_engine"]

    _cleanup_old_jobs()
    with jobs_lock:
        job_id = str(uuid.uuid4())
        is_running = _has_running_job()
        initial_status = "running" if not is_running else "queued"
        initial_phase = "Starting..." if not is_running else "Queued in background"
        jobs[job_id] = {
            "status": initial_status,
            "phase": initial_phase,
            "buffer": None,
            "created_at": time.time(),
            "name": str(input_source),
            "source": str(input_source),
            "language": str(language),
            "tts_engine": str(tts_engine or "edge_tts")
        }
        try:
            create_job(job_id, str(input_source), phase=initial_phase, status=initial_status)
        except Exception as _exc:
            log_suppressed("web_ui.start_pipeline", _exc)

    try:
        worker = _worker_for_engine(engine)
        # Argument order comes from the worker's own signature (queued jobs are stored positionally).
        job_entry = {
            "job_id": job_id,
            "target": worker,
            "args": build_worker_args(worker, job_id, opts),
            "created_at": time.time(),
        }
        if engine in (HARDSUB, SUBTITLE):
            job_entry.update({
                "name": str(opts["project_name"] or input_source),
                "source": str(input_source),
                "language": str(opts["source_language"]),
                "engine_mode": engine,
            })
        elif engine in (SPLITTER, SHORTS):
            job_entry.update({
                "name": f"{'Split' if engine == SPLITTER else 'Shorts'}: {str(opts['project_name'] or input_source)}",
                "source": str(input_source),
                "language": "N/A",
                "tts_engine": "N/A",
                "engine_mode": engine,
            })
        elif engine == DOWNLOAD:
            job_entry.update({
                "name": f"Download: {str(opts['project_name'] or input_source)}",
                "source": str(input_source),
                "language": "N/A",
                "tts_engine": "N/A",
                "engine_mode": DOWNLOAD,
            })
        else:
            job_entry.update({
                "name": str(input_source),
                "source": str(input_source),
                "language": str(language),
                "tts_engine": str(tts_engine or "edge_tts"),
            })

        with queue_lock:
            if not is_running:
                t = threading.Thread(target=job_entry["target"], args=job_entry["args"], daemon=True)
                t.start()
                return {"job_id": job_id, "status": "running"}
            else:
                job_queue.append(job_entry)
                _persist_queued_job(job_entry)
                _ensure_queue_dispatcher()
                pos = len(job_queue)
                return {"job_id": job_id, "status": "queued", "position": pos, "message": f"Job queued at position #{pos}"}
    except Exception as e:
        with jobs_lock:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["phase"] = f"Failed to initialize job: {str(e)}"
            try:
                update_job(job_id, status="error", phase=f"Init error: {str(e)[:60]}")
            except Exception as _exc:
                log_suppressed("web_ui.start_pipeline", _exc)
        raise HTTPException(status_code=500, detail=f"Failed to initialize job: {str(e)}")

@app.post("/api/batch/start")
async def start_batch_pipeline(req: BatchStartRequest):
    inputs = req.inputs
    if not inputs or not all(isinstance(item, str) and item.strip() for item in inputs):
        raise HTTPException(status_code=400, detail="No inputs provided for batch mode")

    engine = normalize_engine(req.engine_mode)
    if engine in (SPLITTER, SHORTS):
        raise HTTPException(status_code=400, detail="The Splitter and Long to Short tools work on one video at a time.")
    opts = resolve_options(engine, req.model_dump())
    language = opts["language"]
    tts_engine = opts["tts_engine"]

    _cleanup_old_jobs()
    with jobs_lock:
        job_id = str(uuid.uuid4())
        is_running = _has_running_job()
        initial_status = "running" if not is_running else "queued"
        initial_phase = "Batch Mode Starting..." if not is_running else "Batch Queued in background"
        jobs[job_id] = {
            "status": initial_status,
            "phase": initial_phase,
            "buffer": None,
            "created_at": time.time(),
            "name": f"Batch ({len(inputs)} items)",
            "source": f"Batch ({len(inputs)} items)",
            "language": str(language),
            "tts_engine": str(tts_engine or "edge_tts")
        }
        try:
            create_job(job_id, f"Batch ({len(inputs)} items)", phase=initial_phase, status=initial_status)
        except Exception as _exc:
            log_suppressed("web_ui.start_batch_pipeline", _exc)

    try:
        job_entry = {
            "job_id": job_id,
            "target": batch_worker,
            "args": build_worker_args(batch_worker, job_id, opts, inputs=inputs),
            "name": f"Batch [{str(opts['engine_mode']).upper()}] ({len(inputs)} items)",
            "source": f"Batch ({len(inputs)} items)",
            "language": str(language),
            "tts_engine": str(tts_engine or "edge_tts"),
            "created_at": time.time()
        }

        with queue_lock:
            if not is_running:
                t = threading.Thread(target=job_entry["target"], args=job_entry["args"], daemon=True)
                t.start()
                return {"job_id": job_id, "status": "running"}
            else:
                job_queue.append(job_entry)
                _persist_queued_job(job_entry)
                _ensure_queue_dispatcher()
                pos = len(job_queue)
                return {"job_id": job_id, "status": "queued", "position": pos, "message": f"Batch job queued at position #{pos}"}
    except Exception as e:
        with jobs_lock:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["phase"] = f"Failed to initialize batch: {str(e)}"
            try:
                update_job(job_id, status="error", phase=f"Init error: {str(e)[:60]}")
            except Exception as _exc:
                log_suppressed("web_ui.start_batch_pipeline", _exc)
        raise HTTPException(status_code=500, detail=f"Failed to initialize batch: {str(e)}")

# =============================================================================
# SECTION 10: SEQUENTIAL FIFO QUEUE MANAGEMENT ENDPOINTS
# (တန်းစီစနစ် ကြည့်ရှုခြင်းနှင့် တန်းစီစာရင်းမှ ပယ်ဖျက်ခြင်း API များ)
# =============================================================================

@app.get("/api/queue")
def get_job_queue():
    """
    Returns list of currently queued jobs, positions, and active job snapshot.
    တန်းစီနေသော အလုပ်များစာရင်း၊ အလှည့်နံပါတ်နှင့် လက်ရှိအလုပ် အခြေအနေကို ပြသပေးသည်။
    """
    with queue_lock:
        items = []
        for idx, item in enumerate(job_queue):
            kwargs = item.get("kwargs", {})
            items.append({
                "job_id": item["job_id"],
                "name": item.get("name", "video"),
                "source": item.get("source") or kwargs.get("input") or item.get("name", "video"),
                "language": item.get("language") or kwargs.get("language", "burmese"),
                "tts_engine": item.get("tts_engine") or kwargs.get("tts_engine", "edge_tts"),
                "position": idx + 1,
                "status": "queued",
                "created_at": item.get("created_at")
            })
    active_info = get_active_job()
    return {
        "queue": items,
        "total": len(items),
        "queue_length": len(items),
        "active_job": active_info if active_info and active_info.get("job_id") else None
    }


@app.delete("/api/queue/{job_id}")
def delete_from_queue(job_id: str):
    """
    Cancels and removes a pending job from the FIFO queue.
    တန်းစီစာရင်းထဲမှ သတ်မှတ်ထားသော အလုပ်အား ဖယ်ရှားပယ်ဖျက်ပေးသည်။
    """
    removed = False
    with queue_lock:
        for idx, item in enumerate(list(job_queue)):
            if item["job_id"] == job_id:
                job_queue.pop(idx)
                removed = True
                break
    with jobs_lock:
        if job_id in jobs and jobs[job_id].get("status") == "queued":
            jobs[job_id]["status"] = "cancelled"
            jobs[job_id]["phase"] = "Cancelled from queue"
            try:
                update_job(job_id, status="cancelled", phase="Cancelled from queue")
            except Exception as _exc:
                log_suppressed("web_ui.delete_from_queue", _exc)
    if removed:
        return {"success": True, "message": f"Job {job_id} removed from queue."}
    raise HTTPException(status_code=404, detail="Job not found in queue")


# =============================================================================
# SECTION 11: PIPELINE STOP & CANCELLATION CONTROLLERS
# (ဗီဒီယို ထုတ်လုပ်မှုကို ရပ်တန့်ခြင်းနှင့် Process သတ်ပစ်ခြင်း API များ)
# =============================================================================

@app.post("/api/stop")
@app.post("/api/cancel")
@app.post("/api/cancel/{job_id}")
async def stop_pipeline(job_id: Optional[str] = None):
    """
    Force-stops running and/or queued jobs.
      - No job_id  : stop everything (running job + whole queue).
      - With job_id: stop ONLY that job. Cancelling a queued job never disturbs the job that is
                     currently rendering (the process-wide stop flag and ffmpeg/demucs kill are
                     applied only when the running job itself is the target).

    job_id မပါပါက အားလုံးကို ရပ်ပြီး၊ ပါပါက ထို job ကိုသာ ရပ်သည်။ တန်းစီနေသော job တစ်ခုကို ဖျက်ရုံဖြင့်
    လက်ရှိ run နေသော job ကို မထိခိုက်စေရန် ဒီအတိုင်း ပြင်ထားသည်။
    """
    stopped_count = 0
    stops_running_job = False
    stopped_running_ids = []

    with jobs_lock:
        if job_id:
            target_jids = [job_id]
        else:
            target_jids = [jid for jid, j in jobs.items() if j.get("status") in ("running", "queued")]

        for jid in target_jids:
            if jid in jobs:
                if jobs[jid].get("status") == "running":
                    stops_running_job = True
                    stopped_running_ids.append(jid)
                cancel_events.setdefault(jid, threading.Event()).set()
                jobs[jid]["status"] = "cancelled"
                jobs[jid]["phase"] = "Stopped by user"
                stopped_count += 1
                try:
                    update_job(jid, status="cancelled", phase="Stopped by user")
                except Exception as _exc:
                    log_suppressed("web_ui.stop_pipeline", _exc)
                print(f"\n[STOP] Force-stop signal received! Cancelled job {jid}.")

    # The env flag is read by every pipeline stage of the *currently running* job.
    if stops_running_job:
        os.environ["CURRENT_JOB_CANCELLED"] = "1"

    with queue_lock:
        if job_id:
            job_queue[:] = [q for q in job_queue if q.get("job_id") != job_id]
        else:
            for q in list(job_queue):
                q_jid = q.get("job_id")
                with jobs_lock:
                    if q_jid in jobs:
                        jobs[q_jid]["status"] = "cancelled"
                        jobs[q_jid]["phase"] = "Stopped by user"
            job_queue.clear()

    # Terminate the processes owned by the job(s) that were stopped: first the exactly-tracked
    # process trees, then a name-based sweep (ffmpeg / ffprobe / yt-dlp / demucs children of this
    # server) as a safety net for processes whose owner could not be resolved. The sweep never
    # touches processes that are registered to a different job (e.g. a standalone download).
    if stops_running_job:
        for jid in stopped_running_ids:
            process_registry.kill_job_processes(jid)
        try:
            import psutil
            spared = set()
            for other in list(jobs.keys()):
                if other not in stopped_running_ids:
                    spared |= process_registry.tracked_pids(other)
            spared |= process_registry.tracked_pids(_STANDALONE_DOWNLOAD_JOB)
            for child in psutil.Process().children(recursive=True):
                try:
                    if child.pid in spared:
                        continue
                    cname = child.name().lower()
                    if any(x in cname for x in ["ffmpeg", "ffprobe", "yt-dlp", "demucs"]):
                        child.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied) as _exc:
                    log_suppressed("web_ui.stop_pipeline", _exc)
        except Exception as _exc:
            log_suppressed("web_ui.stop_pipeline", _exc)

    return {"success": True, "stopped_count": stopped_count, "message": "Pipeline force-stopped successfully."}


# =============================================================================
# SECTION 12: BRANDING & SUBTITLE CONFIGURATION ENDPOINTS
# (Watermark တံဆိပ်နှင့် စာတန်းထိုး Style Setting API များ)
# =============================================================================

@app.get("/api/config/branding")
async def get_branding_config():
    """Returns current watermark overlay and intro title card settings."""
    c = cfg.load_config()
    return {
        "watermark": c.get("watermark", {}),
        "thumbnail_intro": c.get("thumbnail_intro", {"enabled": False, "duration_sec": 3.0}),
    }


@app.post("/api/config/branding")
async def save_branding_config(req: BrandingConfigRequest):
    """Saves customized branding parameters to config.json."""
    c = cfg.load_config()
    if "watermark" not in c:
        c["watermark"] = {}
    c["watermark"]["enabled"] = req.watermark_enabled
    c["watermark"]["text"] = req.watermark_text
    c["watermark"]["opacity"] = req.watermark_opacity
    c["watermark"]["margin"] = req.watermark_margin
    c["watermark"]["font_size"] = req.watermark_font_size
    cfg.save_config(c)
    return {"status": "ok", "watermark": c["watermark"]}


@app.get("/api/config/subtitles")
async def get_subtitle_config():
    """Returns available subtitle style presets and current active setting."""
    c = cfg.load_config()
    sub_cfg = c.get("subtitle_overlay", {})
    return {
        "presets": list(SUBTITLE_PRESETS.values()),
        "current_preset": sub_cfg.get("style_preset", "box_black"),
        "config": sub_cfg,
    }

@app.post("/api/config/subtitles")
async def save_subtitle_config(req: SubtitleConfigRequest):
    """Saves chosen subtitle style preset into config.json."""
    if req.preset not in SUBTITLE_PRESETS:
        raise HTTPException(status_code=400, detail="Unknown preset ID")
    c = cfg.load_config()
    if "subtitle_overlay" not in c:
        c["subtitle_overlay"] = {}
    c["subtitle_overlay"]["style_preset"] = req.preset
    cfg.save_config(c)
    return {"status": "ok", "preset": req.preset}

# =============================================================================
# SECTION 13: REALTIME SSE LOG STREAMING ENDPOINT
# (Console Output များကို Zero-Latency ဖြင့် Real-Time ပို့ဆောင်ပေးသော SSE စနစ်)
# =============================================================================

@app.get("/api/stream/{job_id}")
async def stream_job_logs(job_id: str, request: Request):
    """
    Server-Sent Events (SSE) stream for zero-latency live logs and progress updates.
    Console terminal တွင် ပေါ်သမျှ logs များကို Web Browser UI ဆီသို့ အချိန်နှင့်တပြေးညီ
    တိုက်ရိုက် ပို့ဆောင်ပေးသည့် SSE Endpoint ဖြစ်ပါသည်။
    """
    with jobs_lock:
        if job_id not in jobs:
            raise HTTPException(status_code=404, detail="Job not found")

    async def event_generator():
        loop = asyncio.get_running_loop()
        q = asyncio.Queue()
        sub_item = (q, loop)
        if job_id not in thread_stdout.subscribers:
            thread_stdout.subscribers[job_id] = []
        thread_stdout.subscribers[job_id].append(sub_item)

        try:
            with jobs_lock:
                job = jobs.get(job_id, {})
                buf = job.get('buffer') or thread_stdout.buffers.get(job_id)
                initial_content = buf.getvalue() if buf else ""

            if initial_content:
                lines = [l for l in initial_content.split('\n') if l.strip()]
                for l in lines[-30:]:
                    yield {"event": "log", "data": json.dumps({"line": l})}

            while True:
                if await request.is_disconnected():
                    break

                try:
                    chunk = await asyncio.wait_for(q.get(), timeout=1.5)
                    lines = chunk.replace('\r', '\n').split('\n')
                    for l in lines:
                        if l.strip():
                            current_phase = "Running..."
                            batch_status = None
                            if '--- [Phase' in l or '--- [DONE]' in l:
                                current_phase = l.strip().strip('-').strip()
                            elif '[DONE]' in l:
                                current_phase = 'Done'
                            elif 'Downloading:' in l or 'Downloading video' in l:
                                current_phase = 'Downloading Video...'

                            m = re.search(r'\[(\d+)/(\d+)\] Processing:\s*(.*)', l)
                            if m: batch_status = f"Queue: {m.group(1)} of {m.group(2)} ({m.group(3)})"

                            with jobs_lock:
                                cur_status = jobs.get(job_id, {}).get('status', 'running')

                            yield {
                                "event": "log",
                                "data": json.dumps({
                                    "line": l,
                                    "phase": current_phase,
                                    "batch_status": batch_status,
                                    "status": cur_status
                                })
                            }
                except asyncio.TimeoutError:
                    # Cloudflare keepalive ping to prevent proxy/tunnel dropping the connection
                    yield {"comment": "ping"}

                with jobs_lock:
                    current_job = jobs.get(job_id)
                    if not current_job:
                        break
                    job_status = current_job.get('status')

                if job_status in ('done', 'error', 'cancelled'):
                    # Drain any pending log chunks in queue before sending done event
                    while not q.empty():
                        try:
                            chunk = q.get_nowait()
                            lines = chunk.replace('\r', '\n').split('\n')
                            for l in lines:
                                if l.strip():
                                    yield {
                                        "event": "log",
                                        "data": json.dumps({
                                            "line": l,
                                            "phase": "Done" if job_status == "done" else "Completed",
                                            "batch_status": None,
                                            "status": job_status
                                        })
                                    }
                        except Exception:
                            break

                    yield {
                        "event": "done",
                        "data": json.dumps({"status": job_status, "error": current_job.get("error"), "job_id": job_id,
                                            "package_url": _package_url(current_job)})
                    }
                    break

        finally:
            subs = thread_stdout.subscribers.get(job_id, [])
            if sub_item in subs:
                subs.remove(sub_item)
            if not subs:
                thread_stdout.subscribers.pop(job_id, None)

    return EventSourceResponse(
        event_generator(),
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        }
    )


# =============================================================================
# SECTION 14: JOB STATUS & PROGRESS MONITORING ENDPOINTS
# (လက်ရှိ အလုပ်အခြေအနေနှင့် ရာခိုင်နှုန်း စစ်ဆေးမှု API များ)
# =============================================================================

@app.get("/api/status")
async def latest_status_endpoint():
    """Returns the latest active or most recent job's status."""
    with jobs_lock:
        if not jobs:
            return {"status": "idle", "phase": "Idle", "progress": 0, "job_id": None, "log": []}
        latest_id = list(jobs.keys())[-1]
    return await status_endpoint(latest_id)


def _package_url(job: Dict[str, Any]) -> Optional[str]:
    """Download URL of the CapCut package a finished job built (delivery mode capcut / both), else None."""
    if job.get("status") != "done" or not job.get("package"):
        return None
    return "/api/projects/" + quote(str(job["package"]), safe="") + "/capcut"


@app.get("/api/status/{job_id}")
async def status_endpoint(job_id: str):
    """
    Returns granular status, phase breakdown, percentage, and timing metrics for a job ID.
    Job ID အလိုက် လက်ရှိ ပြီးစီးမှု ရာခိုင်နှုန်း၊ အဆင့်နှင့် အချိန်များကို တွက်ချက်ပေးသည်။
    """
    with jobs_lock:
        if job_id not in jobs:
            raise HTTPException(status_code=404, detail="Job not found")
        job = jobs[job_id]
        
    buffer = job.get('buffer') or thread_stdout.buffers.get(job_id)
    log_lines = []
    progress_marks: List[str] = []
    current_phase = job.get('phase', 'Starting...')
    batch_status = None
    phase_timings = {}
    
    if buffer:
        content = buffer.getvalue()
        # The tools (Splitter, Long to Short) print "[PROGRESS] 0-100"; it drives the bar and is hidden from the log.
        progress_marks = re.findall(r'\[PROGRESS\]\s*(\d{1,3})', content)
        lines = [
            line for line in content.split('\n')
            if line.strip() and '[PROGRESS]' not in line
            and not any(ign in line for ign in ("GET /api/status", "GET /api/queue", "GET /api/keys", "OPTIONS /api/"))
        ]
        log_lines = lines[-35:]

        for line in reversed(lines):
            if '--- [Phase' in line or '--- [DONE]' in line:
                current_phase = line.strip().strip('-').strip()
                break
            if '▶ STEP' in line or 'STEP ' in line:
                current_phase = line.replace('▶', '').strip()
                break
            if '[DONE]' in line or 'PIPELINE COMPLETE' in line:
                current_phase = 'Done'
                break
            if 'Rendering with' in line or 'Compositing' in line:
                current_phase = 'Phase 5: Rendering Video'
                break
            if 'Translating Batch' in line:
                current_phase = 'Phase 3: Translating Dialogue'
                break
            if 'Transcribing' in line or 'Whisper' in line:
                current_phase = 'Phase 2: Extracting Audio & Timestamps'
                break
            if 'Downloading:' in line or 'Downloading video' in line:
                current_phase = 'Phase 1: Downloading Video'
                break

        for line in reversed(lines):
            m = re.search(r'\[(\d+)/(\d+)\] Processing:\s*(.*)', line)
            if m:
                batch_status = f"Queue: {m.group(1)} of {m.group(2)} ({m.group(3)})"
                break
            m2 = re.search(r'\[(\d+)/(\d+)\] Downloading:', line)
            if m2 and not batch_status:
                batch_status = f"Downloading: {m2.group(1)} of {m2.group(2)}"
                break

        # Extract live phase completion durations
        for line in lines:
            if 'TIMING]' in line:
                tm = re.search(r'\[(?:⏱️\s*)?TIMING\] (Phase [^f]+) finished in ([\d\.]+)s', line)
                if tm:
                    phase_timings[tm.group(1).strip()] = float(tm.group(2))

    created_at = job.get("created_at")
    elapsed_sec = round(time.time() - created_at, 1) if created_at else None

    # Compute progress integer (0-100) from phase name for frontend progress bar
    progress_map = {
        "Step 1": 15, "Step 2": 30, "Step 3": 50, "Step 4": 75,
        "Step 5": 82, "Step 6": 90, "Step 7": 98,
        "Phase 1": 15, "Phase 2": 30, "Phase 3": 50, "Phase 4": 75,
        "Phase 5": 90, "Phase 6": 95, "Phase 6b": 98, "Phase 7": 98,
        "Done": 100, "Downloading": 10, "Starting": 3,
    }
    progress = 0
    if job["status"] == "done":
        progress = 100
    elif job["status"] in ("error", "cancelled"):
        progress = 0
    else:
        # Check for dynamic translation batch progress
        batch_found = False
        if buffer:
            for line in reversed(lines[-30:]):
                bm = re.search(r'Translating Batch\s+(\d+)\s*/\s*(\d+)', line)
                if bm:
                    cur_b, tot_b = int(bm.group(1)), max(1, int(bm.group(2)))
                    progress = min(74, int(30 + (cur_b / tot_b) * 44))
                    batch_found = True
                    break
        if progress_marks:
            progress = min(99, int(progress_marks[-1]))
            batch_found = True
        if not batch_found:
            for phase_key, pval in progress_map.items():
                if phase_key.lower() in current_phase.lower():
                    progress = pval
                    break

    zip_url = None
    if job["status"] == "done":
        zip_url = f"/api/download/zip?job_id={job_id}"

    return {
        "job_id": job_id,
        "status": job["status"],
        "phase": current_phase,
        "progress": progress,
        "batch_status": batch_status,
        "elapsed_sec": elapsed_sec,
        "phase_timings": phase_timings,
        "log": log_lines,
        "error": job.get("error"),
        "zip_url": zip_url,
        "package_url": _package_url(job),
        "project_dir": job.get("project_dir"),
    }


# =============================================================================
# SECTION 15: MEDIA OUTPUTS & FILE MANAGEMENT ENDPOINTS
# (ထုတ်လုပ်ပြီး ဗီဒီယို ဖိုင်များ၊ စာတန်းထိုးနှင့် ZIP ဒေါင်းလုဒ် API များ)
# =============================================================================

@app.get("/api/outputs")
def list_outputs():
    metadata = list_movie_states("outputs")
    outputs_dir = "outputs"
    if not os.path.exists(outputs_dir):
        return {"metadata": metadata, "outputs": {}}

    result = {}
    for root, dirs, files in os.walk(outputs_dir):
        if os.path.basename(root) in ["temp", "voiceover"]:
            dirs[:] = []
            continue

        if "state.json" not in files:
            continue

        rel_dir = os.path.relpath(root, outputs_dir).replace(os.sep, "/")
        if rel_dir == ".":
            rel_dir = ""

        entry = {"files": [], "progress": 100, "phase": "Done", "total_duration": "", "phase_durations": {}, "engine_type": "recap"}
        for f in sorted(files):
            if f == "state.json":
                try:
                    with open(os.path.join(root, f), 'r', encoding='utf-8') as sf:
                        state_data = json.load(sf)
                        entry["progress"] = state_data.get("progress", 100)
                        entry["phase"] = state_data.get("current_phase", "Done")
                        entry["total_duration"] = state_data.get("total_duration_formatted", "")
                        entry["total_duration_sec"] = state_data.get("total_duration_sec", 0.0)
                        entry["phase_durations"] = state_data.get("phase_durations", {})
                        entry["engine_type"] = state_data.get("engine_type", "subtitle" if any("subtitle_burmese" in x for x in files) else "recap")
                        entry["total_records"] = state_data.get("total_records")
                except Exception as _exc:
                    log_suppressed("web_ui.list_outputs", _exc)
                continue
            entry["files"].append(posixpath.join(rel_dir, f) if rel_dir else f)

        result[rel_dir or os.path.basename(root)] = entry
        dirs[:] = []

    return {"metadata": metadata, "outputs": result}

@app.get("/api/logs/{movie_name:path}")
def get_movie_logs(movie_name: str):
    safe_name = os.path.normpath(movie_name).strip(" /\\.")
    outputs_dir = os.path.abspath("outputs")
    log_path = os.path.normpath(os.path.join(outputs_dir, safe_name, "pipeline.log"))
    if os.path.commonpath([outputs_dir, log_path]) != outputs_dir:
        raise HTTPException(status_code=400, detail="Invalid log path")
    if not os.path.exists(log_path):
        raise HTTPException(status_code=404, detail="Log file not found for this movie")
    
    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()
    return PlainTextResponse(content)

@app.get("/api/outputs/file")
@app.get("/api/download")
def serve_output(path: str = Query("")):
    rel_path = path
    if not rel_path:
        raise HTTPException(status_code=400, detail="No file path specified")

    safe_path = os.path.normpath(rel_path)
    if safe_path.startswith('..') or os.path.isabs(safe_path):
        raise HTTPException(status_code=400, detail="Invalid file path")

    outputs_dir = os.path.abspath("outputs")
    full_path = os.path.normpath(os.path.join(outputs_dir, safe_path))
    if os.path.commonpath([outputs_dir, full_path]) != outputs_dir:
        raise HTTPException(status_code=400, detail="Invalid file path")
    if not os.path.exists(full_path):
        raise HTTPException(status_code=404, detail="File not found")
        
    if os.path.isdir(full_path):
        files = [
            {"name": f, "url": f"/api/outputs/file?path={quote(os.path.join(rel_path, f))}"}
            for f in sorted(os.listdir(full_path))
        ]
        return {"directory": rel_path, "files": files}

    return FileResponse(full_path)

@app.get("/api/download/zip")
@app.get("/api/outputs/zip")
def download_project_zip(movie: Optional[str] = Query(None), job_id: Optional[str] = Query(None)):
    """Packages all finished recap/hardsub/subtitle assets for a given movie into a single fast-downloadable .zip archive."""
    outputs_dir = os.path.abspath("outputs")
    if not os.path.exists(outputs_dir):
        raise HTTPException(status_code=404, detail="Outputs directory not found")

    selected_dir = None
    safe_movie = None

    # If neither movie nor job_id is provided, or if movie is explicitly empty "" and no job_id
    if not job_id and (movie is None or not movie.strip()):
        raise HTTPException(status_code=400, detail="Movie project name or job_id required")

    # 1. Resolve project directory from movie query
    if movie and movie.strip():
        m_lower = movie.strip().lower()
        if m_lower in ("latest", "recent"):
            subdirs = [
                os.path.join(outputs_dir, d) for d in os.listdir(outputs_dir)
                if os.path.isdir(os.path.join(outputs_dir, d)) and d not in ["voiceover", "temp", "__pycache__"]
            ]
            if not subdirs:
                raise HTTPException(status_code=404, detail="No output projects available")
            subdirs.sort(key=lambda p: os.path.getmtime(p), reverse=True)
            selected_dir = subdirs[0]
            safe_movie = os.path.basename(selected_dir)
        elif m_lower == "all":
            selected_dir = outputs_dir
            safe_movie = "All_Studio_Outputs"
        else:
            cand_movie = os.path.normpath(movie).strip("/\\")
            if cand_movie.startswith("..") or os.path.isabs(cand_movie):
                raise HTTPException(status_code=400, detail="Invalid movie directory")
            cand_path = os.path.normpath(os.path.join(outputs_dir, cand_movie))
            if os.path.commonpath([outputs_dir, cand_path]) != outputs_dir or not os.path.isdir(cand_path):
                raise HTTPException(status_code=404, detail="Movie project output directory not found")
            selected_dir = cand_path
            safe_movie = cand_movie

    # 2. Resolve from job_id if not resolved yet
    elif job_id:
        # Prefer the exact project folder recorded for this job; fall back to the newest one
        # only for jobs that pre-date the recording (e.g. after a server restart).
        with jobs_lock:
            known_job = jobs.get(job_id)
        if known_job is None:
            raise HTTPException(status_code=404, detail="Unknown job id")
        recorded = known_job.get("project_dir")
        job_dir = resolve_inside(outputs_dir, recorded) if recorded else None
        if job_dir and os.path.isdir(job_dir):
            selected_dir = job_dir
            safe_movie = os.path.basename(job_dir)
        else:
            subdirs = [
                os.path.join(outputs_dir, d) for d in os.listdir(outputs_dir)
                if os.path.isdir(os.path.join(outputs_dir, d)) and d not in ["voiceover", "temp", "__pycache__"]
            ]
            if not subdirs:
                raise HTTPException(status_code=404, detail="No output projects available for this job")
            subdirs.sort(key=lambda p: os.path.getmtime(p), reverse=True)
            selected_dir = subdirs[0]
            safe_movie = os.path.basename(selected_dir)

    from starlette.background import BackgroundTask
    temp_dir = os.path.abspath("temp")
    os.makedirs(temp_dir, exist_ok=True)
    clean_base = re.sub(r'[^a-zA-Z0-9_\-]', '_', safe_movie)[:60].strip('_')
    clean_base = clean_base or "Movie"
    zip_filename = f"{clean_base}_Bundle.zip"
    unique_suffix = uuid.uuid4().hex[:8]
    zip_path = os.path.join(temp_dir, f"{clean_base}_{unique_suffix}_bundle.zip")

    excluded_names = {"state.json", "checkpoint.json", "temp", "temp_test_dl"}

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(selected_dir):
            dirs[:] = [d for d in dirs if d not in ["voiceover", "temp", "__pycache__"]]
            for f in sorted(files):
                if f in excluded_names or f.endswith(".tmp") or f.endswith(".part"):
                    continue
                file_full = os.path.join(root, f)
                rel_in_zip = os.path.relpath(file_full, selected_dir)
                # Store pre-compressed video files directly for instant 0-second zipping; compress text/subtitles
                if f.lower().endswith(('.mp4', '.mkv', '.webm', '.avi', '.mov')):
                    zf.write(file_full, arcname=rel_in_zip, compress_type=zipfile.ZIP_STORED)
                else:
                    zf.write(file_full, arcname=rel_in_zip, compress_type=zipfile.ZIP_DEFLATED)

    def _cleanup_temp_zip(p: str):
        try:
            if os.path.exists(p):
                os.remove(p)
        except Exception as _exc:
            log_suppressed("web_ui._cleanup_temp_zip", _exc)

    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=zip_filename,
        headers={"Content-Disposition": f'attachment; filename="{zip_filename}"'},
        background=BackgroundTask(_cleanup_temp_zip, zip_path)
    )

# =============================================================================
# SECTION 16: MOVIE STORAGE, CACHE CLEANUP & SYSTEM CONFIG
# (မူရင်း ဗီဒီယိုစာရင်း၊ Cache ဖျက်သိမ်းခြင်းနှင့် စနစ် Setting API များ)
# =============================================================================

@app.get("/api/movies")
def list_movies():
    """
    Returns list of recognized video files available inside the 'movies/' directory.
    'movies/' ဖိုဒါအတွင်းရှိ ထုတ်လုပ်နိုင်သော ဗီဒီယိုဖိုင်များ စာရင်းကို ထုတ်ပေးသည်။
    """
    movies_dir = "movies"
    if not os.path.exists(movies_dir):
        return []
    files = [f for f in os.listdir(movies_dir) if f.lower().endswith(VIDEO_EXTENSIONS)]
    return sorted(files)


@app.delete("/api/delete/cache")
async def clear_cache():
    """
    Deletes temporary workspace cache files, orphaned MoviePy clips, and Python bytecode.
    စနစ် နှေးကွေးမသွားစေရန် ယာယီ temp ဖိုင်များနှင့် cache များကို ရှင်းထုတ်ပေးသည်။
    """
    cleared = 0
    for d in ["temp", "voiceover"]:
        if os.path.exists(d):
            for item in os.listdir(d):
                p = os.path.join(d, item)
                try:
                    if os.path.isdir(p):
                        shutil.rmtree(p, ignore_errors=True)
                    else:
                        os.remove(p)
                    cleared += 1
                except Exception as _exc:
                    log_suppressed("web_ui.clear_cache", _exc)

    for root, dirs, files in os.walk('.'):
        dirs[:] = [d for d in dirs if d not in ('.venv', 'venv', '.git', 'node_modules')]
        for d in list(dirs):
            if d == '__pycache__':
                try:
                    shutil.rmtree(os.path.join(root, d), ignore_errors=True)
                    dirs.remove(d)
                    cleared += 1
                except Exception as _exc:
                    log_suppressed("web_ui.clear_cache", _exc)

    # Clean any MoviePy intermediate temp files in root/cwd
    for f in os.listdir("."):
        if "TEMP_MPY" in f and (f.endswith(".mp4") or f.endswith(".wav")):
            try:
                os.remove(f)
                cleared += 1
            except Exception as _exc:
                log_suppressed("web_ui.clear_cache", _exc)

    return {"success": True, "cleared_items": cleared}


@app.api_route("/api/config", methods=["GET", "POST"])
async def handle_config(request: Request):
    """
    Retrieves or updates global pipeline configuration settings without exposing raw Gemini keys.
    စနစ်တစ်ခုလုံး၏ Setting များကို ကြည့်ရှု/ပြင်ဆင်ပေးပြီး API Keys များကို ဖုံးကွယ်ပေးထားသည်။
    """
    import brain.config as cfg
    config_data = cfg.load_config()
    if request.method == 'POST':
        data = await request.json() or {}
        if "mirror_video" in data:
            if "copyright_protection" not in config_data:
                config_data["copyright_protection"] = {}
            config_data["copyright_protection"]["mirror_video"] = bool(data["mirror_video"])
            cfg.save_config(config_data)
        if "tts_engine" in data:
            if "voice" not in config_data:
                config_data["voice"] = {}
            config_data["voice"]["engine"] = str(data["tts_engine"]).strip().lower()
            cfg.save_config(config_data)
        if "thumbnail_intro" in data:
            if "thumbnail_intro" not in config_data:
                config_data["thumbnail_intro"] = {}
            if isinstance(data["thumbnail_intro"], dict):
                config_data["thumbnail_intro"].update(
                    {k: v for k, v in data["thumbnail_intro"].items() if k in ("enabled", "duration_sec")}
                )
            elif isinstance(data["thumbnail_intro"], bool):
                config_data["thumbnail_intro"]["enabled"] = data["thumbnail_intro"]
            cfg.save_config(config_data)
        if "audio_ducking" in data:
            if "audio_ducking" not in config_data:
                config_data["audio_ducking"] = {}
            if isinstance(data["audio_ducking"], dict):
                config_data["audio_ducking"].update(
                    {k: v for k, v in data["audio_ducking"].items() if k in ("enabled", "duck_volume", "ambient_volume")}
                )
            elif isinstance(data["audio_ducking"], bool):
                config_data["audio_ducking"]["enabled"] = data["audio_ducking"]
            cfg.save_config(config_data)
        if "use_demucs" in data:
            if "pipeline" not in config_data:
                config_data["pipeline"] = {}
            config_data["pipeline"]["use_demucs"] = bool(data["use_demucs"])
            cfg.save_config(config_data)
        if "scene_detection" in data:
            if "pipeline" not in config_data:
                config_data["pipeline"] = {}
            config_data["pipeline"]["scene_detection"] = bool(data["scene_detection"])
            cfg.save_config(config_data)
        if "script_engine" in data:
            if "pipeline" not in config_data:
                config_data["pipeline"] = {}
            config_data["pipeline"]["script_engine"] = str(data["script_engine"]).strip().lower()
            cfg.save_config(config_data)
        public_config = json.loads(json.dumps(config_data))
        public_config.get("gemini", {}).pop("api_keys", None)
        return {"success": True, "config": public_config}
    else:
        public_config = json.loads(json.dumps(config_data))
        public_config.get("gemini", {}).pop("api_keys", None)
        return public_config


# =============================================================================
# SECTION 17: GEMINI API KEY MANAGEMENT & SECURITY
# (Gemini API Key လုံခြုံရေး၊ Mask ပြုလုပ်ခြင်းနှင့် စစ်ဆေးခြင်း API များ)
# =============================================================================

@app.get("/api/keys/status")
def get_key_status():
    try:
        config_data = cfg.load_config()
        gemini_cfg = config_data.get("gemini", {})
        configured_keys = gemini_cfg.get("api_keys") or os.getenv("GEMINI_API_KEY") or []
        if isinstance(configured_keys, str):
            configured_keys = [k.strip() for k in configured_keys.split(",") if k.strip()]
        elif not isinstance(configured_keys, list):
            configured_keys = []
            
        from brain.tracker import load_usage_db, get_google_utc_date
        db = load_usage_db()
        recorded_keys = db.get("keys", {})
        current_utc = get_google_utc_date()
        
        key_statuses = []
        total_remaining = 0
        
        ping_results = _ping_gemini_keys([str(k) for k in configured_keys], 8.0)

        for idx, key in enumerate(configured_keys):
            if not key or not str(key).strip(): continue
            key_str = str(key).strip()
            masked = key_str[:6] + "..." + key_str[-4:] if len(key_str) > 10 else key_str
            
            live_state, http_code = ping_results[idx]
            kdata = recorded_keys.get(masked, {})
            
            model_limits = gemini_cfg.get("model_limits", {})
            models_data = kdata.get("models", {})
            
            model_stats = []
            for m_name, m_limit in model_limits.items():
                used = models_data.get(m_name, {}).get("used_today", 0)
                if kdata.get("utc_date") != current_utc:
                    used = 0
                remaining = max(0, int(m_limit) - used)
                total_remaining += remaining
                model_stats.append({
                    "name": m_name,
                    "used_today": used,
                    "limit": int(m_limit),
                    "remaining": remaining
                })
                
            key_statuses.append({
                "key": masked,
                "live_status": live_state,
                "http_code": http_code,
                "models": model_stats
            })
            
        hardware = detect_hardware_encoder()
        return {
            "total_keys": len(key_statuses),
            "total_remaining_requests": total_remaining,
            "daily_limit_per_key": int(gemini_cfg.get("daily_limit_per_key", 20)),
            "hardware_encoder": hardware,
            "keys": key_statuses
        }
    except Exception as e:
        print(f"[ERROR] /api/keys/status failed: {e}")
        return {
            "total_keys": 0,
            "total_remaining_requests": 0,
            "daily_limit_per_key": 20,
            "hardware_encoder": {"codec": "libx264", "label": "CPU Multi-Core", "type": "cpu"},
            "keys": [],
            "error": str(e)
        }

@app.delete("/api/delete/{folder_type}/{item_name:path}")
async def delete_item(folder_type: str, item_name: str):
    with jobs_lock:
        if _has_running_job():
            raise HTTPException(status_code=409, detail="Cannot delete files while a pipeline job is running.")
    target_path = _safe_child_path(folder_type, item_name)
    if not target_path:
        raise HTTPException(status_code=400, detail="Invalid target")
    if not os.path.exists(target_path):
        raise HTTPException(status_code=404, detail="Item not found")
        
    try:
        if os.path.isdir(target_path):
            shutil.rmtree(target_path, ignore_errors=True)
        else:
            os.remove(target_path)
            
        if folder_type == "outputs":
            temp_path = os.path.join("temp", item_name)
            if os.path.exists(temp_path):
                shutil.rmtree(temp_path, ignore_errors=True)
            try:
                delete_movie_state(item_name)
            except Exception as e:
                print(f"[WARN] Could not remove output DB entry for {item_name}: {e}")
                
        print(f"[*] WebUI: Successfully deleted {folder_type}/{item_name}")
        return {"success": True, "message": f"Deleted {item_name}"}
    except Exception as e:
        print(f"[!] WebUI: Delete failed ({e})")
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/delete_all/{folder_type}")
async def delete_all(folder_type: str, confirm: bool = False):
    with jobs_lock:
        if _has_running_job():
            raise HTTPException(status_code=409, detail="Cannot delete files while a pipeline job is running.")
    if not confirm:
        raise HTTPException(status_code=400, detail="Mass deletion requires 'confirm=true' parameter.")
    if folder_type not in {'movies', 'outputs', 'temp', 'all'}:
        raise HTTPException(status_code=400, detail="Invalid target")
    
    targets = ['movies', 'outputs', 'temp'] if folder_type == 'all' else [folder_type]
    deleted = 0
    try:
        for target_folder in targets:
            root = os.path.abspath(target_folder)
            if not os.path.isdir(root):
                continue
            for item_name in os.listdir(root):
                if target_folder == 'outputs' and item_name in ['api_usage_db.json', 'movie_metadata.db']:
                    continue
                    
                target = _safe_child_path(target_folder, item_name)
                if not target:
                    continue
                if os.path.isdir(target):
                    shutil.rmtree(target)
                else:
                    os.remove(target)
                deleted += 1
        return {"success": True, "deleted_items": deleted}
    except OSError as error:
        raise HTTPException(status_code=500, detail=f"Could not delete all items: {error}")

@app.post("/api/rename/movie")
async def rename_movie(req: RenameRequest):
    old_name = os.path.basename(req.old_name.strip())
    new_name = os.path.basename(req.new_name.strip())
    if not old_name or not new_name or old_name != req.old_name or new_name != req.new_name:
        raise HTTPException(status_code=400, detail="Use a filename only; folders are not allowed.")
    if not old_name.lower().endswith(VIDEO_EXTENSIONS) or not new_name.lower().endswith(VIDEO_EXTENSIONS):
        raise HTTPException(status_code=400, detail="Movie files must use a supported video extension.")
        
    source = _safe_child_path('movies', old_name)
    destination = _safe_child_path('movies', new_name)
    if not source or not destination or not os.path.isfile(source):
        raise HTTPException(status_code=404, detail="Source movie was not found.")
    if os.path.exists(destination):
        raise HTTPException(status_code=409, detail="A movie with that name already exists.")
    try:
        os.replace(source, destination)
        return {"success": True, "name": new_name}
    except OSError as error:
        raise HTTPException(status_code=500, detail=f"Could not rename movie: {error}")

@app.get("/api/keys")
@app.get("/api/keys/list")
def list_raw_keys():
    try:
        config_data = cfg.load_config()
        gemini_cfg = config_data.get("gemini", {})
        keys = gemini_cfg.get("api_keys", [])
        if isinstance(keys, str):
            keys = [k.strip() for k in keys.split(",") if k.strip()]
        elif not isinstance(keys, list):
            keys = []
        if not keys and os.getenv("GEMINI_API_KEY"):
            keys = [k.strip() for k in os.getenv("GEMINI_API_KEY", "").split(",") if k.strip()]
        masked_keys = [k[:6] + "..." + k[-4:] if len(k) > 10 else k for k in keys]
        return {'keys': masked_keys, 'count': len(masked_keys)}
    except Exception as e:
        print(f"[ERROR] /api/keys/list failed: {e}")
        return {'keys': [], 'count': 0, 'error': str(e)}

@app.post("/api/keys/save")
def save_keys(req: SaveKeysRequest):
    try:
        config_data = cfg.load_config()
        existing_keys = config_data.get("gemini", {}).get("api_keys", [])
        if isinstance(existing_keys, str):
            existing_keys = [k.strip() for k in existing_keys.split(",") if k.strip()]
        elif not isinstance(existing_keys, list):
            existing_keys = []

        # A line may be a full key or the mask of a stored one (resolved so a real key is never lost).
        cleaned_keys, unresolved_masks = resolve_key_lines(req.keys, existing_keys)

        if unresolved_masks:
            # Never "save what we understood": that would silently delete the keys behind the masks.
            raise HTTPException(
                status_code=400,
                detail=f"{len(unresolved_masks)} masked key(s) do not match a stored key. "
                       "Paste the full key, or leave the masked line exactly as shown. Nothing was saved.",
            )

        config_data.setdefault("gemini", {})["api_keys"] = cleaned_keys
        cfg.save_config(config_data)
            
        print(f"[*] WebUI: Updated API keys in config.json ({len(cleaned_keys)} keys)")
        # Permanent Google Drive Sync for Colab
        drive_out = "/content/drive/MyDrive/MovieRecapOutputs"
        if os.path.exists(drive_out):
            try:
                import shutil
                shutil.copy2("config.json", os.path.join(drive_out, "config.json"))
                for db_name in ["movie_metadata.db", "database.db"]:
                    for src_path in [os.path.join("outputs", db_name), db_name]:
                        if os.path.exists(src_path):
                            shutil.copy2(src_path, os.path.join(drive_out, db_name))
                print("[*] WebUI: Permanently synced config.json and database to Google Drive!")
            except Exception as _exc:
                log_suppressed("web_ui.save_keys", _exc)

        # Only masked values leave the server – raw keys are never echoed back.
        masked = [k[:6] + "..." + k[-4:] if len(k) > 10 else "***" for k in cleaned_keys]
        return {"success": True, "keys_count": len(cleaned_keys), "keys": masked}
    except HTTPException:
        raise  # validation errors (400) must not be turned into a 500
    except Exception as e:
        print(f"[ERROR] Failed to save API keys: {e}")
        raise HTTPException(status_code=500, detail=str(e))


MAX_KEYS_PER_CHECK = 20


@app.post("/api/keys/check")
def check_keys(req: SaveKeysRequest):
    """
    Tests pasted Gemini keys against Google WITHOUT saving them (so a typo or a revoked key is found
    before a job depends on it). Masked lines resolve to the stored key they name. Only masked keys are
    returned and nothing is persisted or logged.
    """
    stored = cfg.load_config().get("gemini", {}).get("api_keys", [])
    stored = [k.strip() for k in stored.split(",") if k.strip()] if isinstance(stored, str) else (stored if isinstance(stored, list) else [])
    keys, unresolved = resolve_key_lines(req.keys, stored)
    if unresolved:
        raise HTTPException(status_code=400, detail=f"{len(unresolved)} masked key(s) do not match a stored key. Paste the full key.")
    if not keys:
        raise HTTPException(status_code=400, detail="No keys to check.")
    if len(keys) > MAX_KEYS_PER_CHECK:
        raise HTTPException(status_code=400, detail=f"Check at most {MAX_KEYS_PER_CHECK} keys at a time.")
    results = _ping_gemini_keys(keys, 8.0)
    return {"results": [
        {"key": mask_key(key), "state": state, "http_code": code, "ok": state == "ONLINE"}
        for key, (state, code) in zip(keys, results)
    ]}


# =============================================================================
# SECTION 18: SUBTITLE PREVIEW & VISUAL AUDIT ENDPOINTS
# (စာတန်းထိုး Preview ကြည့်ရှုခြင်းနှင့် အရည်အသွေး စစ်ဆေးမှု API)
# =============================================================================

@app.get("/api/subtitle/preview/{movie_name:path}")
def preview_subtitle_project(movie_name: str):
    """
    Returns SRT subtitle text, Burmese transcript, Quality Check (QC) report,
    and structured dialogue records for the frontend interactive subtitle editor/previewer.
    
    စာတန်းထိုး ပြန်လည်စစ်ဆေးတည်းဖြတ်နိုင်ရန် SRT၊ Transcript၊ QC Report နှင့် Records များကို ထုတ်ပေးသည်။
    """
    safe_name = os.path.normpath(movie_name).strip(" /\\.")
    outputs_dir = os.path.abspath("outputs")
    proj_dir = os.path.normpath(os.path.join(outputs_dir, safe_name))
    if os.path.commonpath([outputs_dir, proj_dir]) != outputs_dir or not os.path.exists(proj_dir):
        raise HTTPException(status_code=404, detail="Project not found")

    srt_file = os.path.join(proj_dir, "05_subtitle_burmese.srt")
    qc_file = os.path.join(proj_dir, "06_quality_check_report.txt")
    if not os.path.exists(qc_file):
        alt_qc = os.path.join(proj_dir, "06_translation_qc_report.txt")
        if os.path.exists(alt_qc):
            qc_file = alt_qc
    txt_file = os.path.join(proj_dir, "04_transcript_burmese.txt")
    json_file = os.path.join(proj_dir, "records_data.json")

    srt_content = ""
    if os.path.exists(srt_file):
        with open(srt_file, "r", encoding="utf-8", errors="replace") as f:
            srt_content = f.read()

    qc_content = ""
    if os.path.exists(qc_file):
        with open(qc_file, "r", encoding="utf-8", errors="replace") as f:
            qc_content = f.read()

    txt_content = ""
    if os.path.exists(txt_file):
        with open(txt_file, "r", encoding="utf-8", errors="replace") as f:
            txt_content = f.read()

    raw_records = []
    if os.path.exists(json_file):
        try:
            with open(json_file, "r", encoding="utf-8", errors="replace") as f:
                raw_records = json.load(f)
        except Exception as _exc:
            log_suppressed("web_ui.preview_subtitle_project", _exc)

    normalized_records = []
    for r in raw_records[:300]:
        s_time = r.get("start_s") if r.get("start_s") is not None else r.get("start_time", 0.0)
        e_time = r.get("end_s") if r.get("end_s") is not None else r.get("end_time", 0.0)
        orig = r.get("original") or r.get("original_text") or ""
        burm = r.get("burmese") or r.get("translated_text") or ""
        normalized_records.append({
            "start_time": float(s_time or 0.0),
            "end_time": float(e_time or 0.0),
            "original_text": str(orig),
            "translated_text": str(burm),
            "speaker_gender": r.get("speaker_gender") or r.get("gender") or "",
        })

    return {
        "project": safe_name,
        "srt": srt_content,
        "txt": txt_content,
        "qc": qc_content,
        "srt_content": srt_content,
        "qc_report": qc_content,
        "transcript_burmese": txt_content,
        "records": normalized_records,
        "total_records": len(raw_records)
    }


# =============================================================================
# SECTION 19: SERVER ENTRYPOINT & CLI ARGUMENT PARSER
# (Web Server စတင်နှိုးဆော်ခြင်းနှင့် Command-Line Option စီမံမှု)
# =============================================================================

if __name__ == '__main__':
    import uvicorn
    import argparse

    parser = argparse.ArgumentParser(description="Pai AI Movie Studio Web UI Server")
    parser.add_argument("--host", type=str, default=None, help="Host address to bind the server to")
    parser.add_argument("--port", type=int, default=None, help="Port number to listen on")
    args, _ = parser.parse_known_args()

    # Automatically detect cloud notebook environments (Google Colab, Kaggle)
    is_cloud_env = ("COLAB_GPU" in os.environ or "KAGGLE_KERNEL_RUN_TYPE" in os.environ or "COLAB_RELEASE_TAG" in os.environ)
    default_host = "0.0.0.0" if is_cloud_env else "127.0.0.1"
    host = args.host or os.getenv("HOST", default_host)
    port = args.port or int(os.getenv("PORT", 5000))

    # Exposed beyond this machine (Colab / Kaggle / Docker / --host 0.0.0.0) without a password?
    # Generate a one-time token instead of serving the whole API anonymously.
    app.state.bind_host = host
    generated_token = security.ensure_remote_access_secret(host)
    if generated_token:
        print("[SECURITY] Server is reachable from the network and WEB_UI_PASSWORD / WEB_UI_TOKEN is not set.")
        print(f"[SECURITY] A random access token was generated for this session: {generated_token}")
        print(f"[SECURITY] Open: http://<server-address>:{port}/?token={generated_token}")

    print(f"[*] Starting Pai AI Movie Studio Web Server on http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level='info')
