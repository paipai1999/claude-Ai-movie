"""
Web Security Helpers (Web UI လုံခြုံရေး အထောက်အကူ function များ)
==============================================================
Authentication, same-origin and bind-address policy for the dashboard API.
Kept free of FastAPI route code so it can be unit-tested in isolation.

Policy / မူဝါဒ:
  1. A secret (WEB_UI_PASSWORD / WEB_UI_TOKEN) is compared in constant time.
  2. The browser session cookie stores a SHA-256 digest of the secret, never the secret itself.
  3. State-changing requests that carry an `Origin` header must come from the same host
     (or an explicitly allowed origin) -> blocks cross-site (CSRF / drive-by) calls.
  4. When the server listens on a non-loopback address and no secret is configured,
     a random token is generated so the API is never exposed anonymously.
"""

import hashlib
import hmac
import os
import secrets
from typing import Iterable, Optional
from urllib.parse import urlsplit

# HTTP methods that only read data (no state change) are exempt from the Origin check.
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


def get_auth_secret() -> str:
    """Returns the configured dashboard secret ('' when authentication is disabled)."""
    return os.getenv("WEB_UI_PASSWORD") or os.getenv("WEB_UI_TOKEN") or ""


def secrets_match(supplied: Optional[str], expected: str) -> bool:
    """Constant-time string comparison (timing-attack safe)."""
    if not supplied or not expected:
        return False
    return hmac.compare_digest(supplied.strip().encode("utf-8"), expected.encode("utf-8"))


def session_cookie_value(secret: str) -> str:
    """Opaque cookie value derived from the secret (the raw secret is never stored client-side)."""
    return hashlib.sha256(("pai-ai-studio-session:" + secret).encode("utf-8")).hexdigest()


def is_loopback_host(host: Optional[str]) -> bool:
    """True for addresses that are only reachable from the same machine."""
    return str(host or "").strip().lower() in LOOPBACK_HOSTS


def ensure_remote_access_secret(bind_host: str) -> Optional[str]:
    """
    If the server is exposed beyond loopback with no secret configured, generate a random
    token (exported through WEB_UI_TOKEN) and return it so the caller can print it once.
    Returns None when nothing had to be generated.

    Loopback မဟုတ်သော address ဖြင့် ဖွင့်ပြီး password မရှိပါက random token ကို အလိုအလျောက် ထုတ်ပေးသည်။
    """
    if is_loopback_host(bind_host) or get_auth_secret():
        return None
    token = secrets.token_urlsafe(18)
    os.environ["WEB_UI_TOKEN"] = token
    return token


def parse_allowed_origins(raw: Optional[str]) -> list:
    """Parses WEB_UI_ALLOWED_ORIGINS into a clean list ('*' stays '*')."""
    return [o.strip().rstrip("/") for o in str(raw or "").split(",") if o.strip()]


def _origin_netloc(origin: str) -> str:
    return urlsplit(origin).netloc.lower()


def origin_is_allowed(origin: Optional[str], request_host: Optional[str], allowed: Iterable[str]) -> bool:
    """
    Decides whether a request with the given `Origin` header may change state.
    - No Origin header (curl, scripts, same-site navigation) -> allowed.
    - Origin host == Host header (the dashboard itself)       -> allowed.
    - Origin explicitly listed in WEB_UI_ALLOWED_ORIGINS       -> allowed.
    """
    if not origin:
        return True
    if origin == "null":
        # 'null' is sent by sandboxed iframes / file:// pages – treat as hostile.
        return False
    allowed = list(allowed or [])
    if "*" in allowed:
        return True
    if origin.rstrip("/") in allowed:
        return True
    return _origin_netloc(origin) == str(request_host or "").lower()


def host_is_trusted(request_host: Optional[str], bind_host: Optional[str], extra_hosts: Iterable[str] = ()) -> bool:
    """
    DNS-rebinding guard: when the server is bound to loopback only, the `Host` header must
    be a loopback name (or explicitly trusted). For public binds there is no fixed hostname,
    so authentication is the protection and every Host is accepted.
    """
    if bind_host is None or not is_loopback_host(bind_host):
        return True
    name = str(request_host or "").lower()
    if name.startswith("["):  # [::1]:5000
        name = name.split("]")[0] + "]"
    else:
        name = name.split(":")[0]
    trusted = set(LOOPBACK_HOSTS) | {h.strip().lower() for h in extra_hosts if h and h.strip()}
    return name in trusted
