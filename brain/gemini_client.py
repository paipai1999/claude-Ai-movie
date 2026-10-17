from brain.diagnostics import log_suppressed
import json
import time
import os
import urllib.request
import urllib.error
from typing import Union, List

def _mask_key(key: str) -> str:
    """Mask API key for safe logging."""
    if not key or len(key) <= 8:
        return '***'
    return key[:6] + '...' + key[-4:]

def _interruptible_sleep(seconds: float) -> None:
    """
    time.sleep() that wakes up as soon as the user presses Stop (CURRENT_JOB_CANCELLED=1) and then
    raises InterruptedError, so a 65-second rate-limit wait can never keep a cancelled job alive.

    Stop နှိပ်လျှင် rate-limit စောင့်ချိန်ကို ချက်ချင်း ဖြတ်ပြီး InterruptedError ပေးသည်။
    """
    deadline = time.time() + max(0.0, float(seconds))
    while True:
        if os.environ.get("CURRENT_JOB_CANCELLED") == "1":
            raise InterruptedError("Gemini request cancelled by user.")
        remaining = deadline - time.time()
        if remaining <= 0:
            return
        time.sleep(min(0.5, remaining))


# ─────────────────────────────────────────────────────────────────────────────
# Valid Google AI Studio Gemini Models (Priority Order):
# 1. gemini-3.1-flash-lite    : Ultra-fast verified workhorse (~1.3s latency)
# 2. gemini-3.6-flash         : High-performance flagship model (Verified 200 OK)
# 3. gemini-flash-latest      : Always updated latest flash model (Verified 200 OK)
# 4. gemini-flash-lite-latest : Always updated latest flash lite model
# 5. gemini-3.7-flash         : Advanced reasoning & translation
# 6. gemini-3.8-flash         : Heavy reasoning model
# 7. gemini-3-flash-preview   : High-Speed Preview Model
# 8. gemini-3.5-flash-lite    : Ultra Fast Lite Model
# 9. gemini-3.5-flash         : Quality model
# 10. gemini-2.5-flash        : Legacy 2.5 series
# 11. gemini-2.0-flash        : Legacy 2.0 series
# 12. gemini-1.5-flash        : Legacy 1.5 series
_FALLBACK_MODELS = [
    "gemini-3.1-flash-lite",
    "gemini-3.6-flash",
    "gemini-flash-latest",
    "gemini-flash-lite-latest",
    "gemini-3.7-flash",
    "gemini-3.8-flash",
    "gemini-3-flash-preview",
    "gemini-3.5-flash-lite",
    "gemini-3.5-flash",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-1.5-flash",
]

# How many seconds to wait when ALL keys are rate-limited before retrying.
# Must be >= 60 to let the per-minute quota window reset.
_RPM_WAIT_SEC = 65


class TruncatedResponseError(Exception):
    """Gemini stopped at maxOutputTokens, so a JSON payload is cut off mid-array.

    Retrying the same prompt on another key or model hits the same budget, so callers
    should shrink the request (e.g. split the batch) instead.
    """


def _extract_text_from_gemini_response(res_data: dict, expect_json: bool = False) -> str:
    """Safely extracts text from Gemini REST response, handling multi-part, thoughts, and finishReason.

    With expect_json=True a MAX_TOKENS finish raises TruncatedResponseError instead of
    returning half a JSON document.
    """
    candidates = res_data.get("candidates", [])
    if not candidates:
        prompt_feedback = res_data.get("promptFeedback", {})
        block_reason = prompt_feedback.get("blockReason", "NO_CANDIDATES")
        raise Exception(f"Gemini API blocked response: {block_reason}")

    cand = candidates[0]
    content = cand.get("content") or {}
    parts = content.get("parts") or []
    
    text_chunks = []
    for p in parts:
        if isinstance(p, dict):
            # FIX: Gemini 2.5/3.x models output Chain-of-Thought reasoning with 'thought': True
            # Filtering these parts out prevents internal reasoning from corrupting JSON payloads.
            if p.get("thought") or p.get("role") == "thought" or p.get("type") == "thought":
                continue
            if p.get("text"):
                text_chunks.append(p["text"])
        elif isinstance(p, str):
            text_chunks.append(p)

    final_text = "".join(text_chunks).strip()
    
    # Strip any inline thinking tags if present (e.g. <thought>...</thought>, <think>...</think>)
    import re
    final_text = re.sub(r'<thought>.*?</thought>', '', final_text, flags=re.DOTALL | re.IGNORECASE).strip()
    final_text = re.sub(r'<think>.*?</think>', '', final_text, flags=re.DOTALL | re.IGNORECASE).strip()

    if not final_text:
        finish_reason = cand.get("finishReason", "EMPTY_RESPONSE")
        if expect_json and finish_reason == "MAX_TOKENS":
            raise TruncatedResponseError("Gemini hit maxOutputTokens before writing any text (finishReason=MAX_TOKENS)")
        raise Exception(f"Gemini returned empty text response (finishReason={finish_reason})")

    if expect_json and cand.get("finishReason") == "MAX_TOKENS":
        raise TruncatedResponseError(
            f"Gemini output was cut off at maxOutputTokens ({len(final_text)} chars received)"
        )

    return final_text


def call_gemini(
    system_prompt: str,
    user_prompt: str,
    api_key: Union[str, List[str]],
    model: str = "gemini-3.5-flash-lite",
    temperature: float = 0.7,
    max_tokens: int = 4096,
    response_mime_type: str = "application/json",  # FIX-BUG3: allow "text/plain" for non-JSON callers
    images: List[Union[str, dict]] = None,
) -> tuple:
    """Shared Gemini API client with automatic model fallback and API key rotation.

    Args:
        response_mime_type: "application/json" (default) or "text/plain" for plain-text prompts.
            Use "text/plain" when the prompt asks Gemini to return raw text (not JSON).
        images: Optional list of base64 JPEG strings or dicts with {"mimeType": ..., "data": ...}
            for multimodal visual reasoning.

    Returns:
        (text, used_model) tuple on success.
    Raises:
        The last exception on total failure.
    """
    api_keys = _normalize_keys(api_key)

    # Build de-duplicated model list (requested model first, then fallbacks)
    models_to_try = _build_model_list(model)

    last_err = None

    import brain.config as cfg
    from brain.tracker import reserve_model_usage
    model_limits = cfg.load_config().get("gemini", {}).get("model_limits", {})

    # Up to 3 full rotation attempts (with 65-second RPM-reset waits in between)
    for attempt in range(3):
        for m in models_to_try:
            limit = int(model_limits.get(m, 500 if "flash" in m else 50))
            model_404 = False  # FIX-W2: track per-model 404 to skip whole model
            for key in api_keys:
                if model_404:
                    break  # FIX-W2: 404 = model unavailable globally → skip to next model
                key = str(key).strip()
                if not key:
                    continue
                
                if not reserve_model_usage(key, m, limit):
                    continue

                url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent"
                gen_config = {
                    "temperature": temperature,
                    "maxOutputTokens": max_tokens,
                }
                # FIX-BUG3: Only set responseMimeType when caller explicitly wants JSON.
                # Setting it to "application/json" on plain-text prompts forces Gemini to
                # wrap its response in JSON even when the prompt says "Return ONLY the text".
                if response_mime_type:
                    gen_config["responseMimeType"] = response_mime_type

                user_parts = [{"text": user_prompt}]
                if images:
                    for img in images:
                        if isinstance(img, dict) and "data" in img:
                            user_parts.append({
                                "inlineData": {
                                    "mimeType": img.get("mimeType", "image/jpeg"),
                                    "data": img["data"]
                                }
                            })
                        elif isinstance(img, str) and img:
                            user_parts.append({
                                "inlineData": {
                                    "mimeType": "image/jpeg",
                                    "data": img
                                }
                            })

                payload = {
                    "system_instruction": {"parts": [{"text": system_prompt}]},
                    "contents": [{"role": "user", "parts": user_parts}],
                    "generationConfig": gen_config,
                }

                try:
                    req = urllib.request.Request(
                        url,
                        data=json.dumps(payload).encode("utf-8"),
                        headers={"Content-Type": "application/json", "x-goog-api-key": key},
                        method="POST",
                    )
                    api_timeout = 50.0 if images else 35.0
                    with urllib.request.urlopen(req, timeout=api_timeout) as response:
                        res_data = json.loads(response.read().decode("utf-8"))
                        _record_api_usage(key, m, "success")
                        text = _extract_text_from_gemini_response(
                            res_data, expect_json=response_mime_type == "application/json"
                        )
                        return text, m

                except TruncatedResponseError:
                    raise   # same budget on every key/model; the caller must shrink the request

                except urllib.error.HTTPError as e:
                    last_err = e
                    err_body = ""
                    try: err_body = e.read().decode("utf-8")
                    except Exception as _exc:
                        log_suppressed("brain.gemini_client.call_gemini", _exc)
                    
                    if e.code == 429:
                        is_daily = "daily" in err_body.lower() or "per day" in err_body.lower() or "free_tier_requests" in err_body.lower()
                        setattr(e, "is_quota", is_daily)
                        if is_daily:
                            print(f"[!] Gemini API Daily Quota Exhausted (429) on '{m}'. Trying next API key...")
                        else:
                            print(f"[!] Gemini API Rate Limit (429 RPM) on '{m}'. Trying next API key...")
                        _record_api_usage(key, m, "rate_limited")
                        _interruptible_sleep(2)   # brief pause — don't hammer
                        continue        # next key, same model
                    elif e.code in (404, 503):
                        # FIX-W2: 404/503 = model doesn't exist or service unavailable globally → skip whole model
                        print(
                            f"[!] Gemini API model '{m}' returned HTTP {e.code}. "
                            f"Skipping model and trying next fallback model..."
                        )
                        _record_api_usage(key, m, f"error_{e.code}")
                        model_404 = True
                        break           # FIX-W2: exit key loop → outer for-m loop moves to next model
                    else:
                        print(
                            f"[!] Gemini API model '{m}' returned HTTP {e.code} on key '{_mask_key(key)}'. "
                            f"Trying next key..."
                        )
                        _record_api_usage(key, m, f"error_{e.code}")
                        continue        # next key, same model

                except (TimeoutError, urllib.error.URLError) as e:
                    last_err = e
                    _record_api_usage(key, m, "timeout")
                    print(f"[!] Gemini API model '{m}' timed out or connection error on key '{_mask_key(key)}': {e}. Trying next key...")
                    continue

                except Exception as e:
                    last_err = e
                    _record_api_usage(key, m, "error")
                    print(f"[!] Gemini API model '{m}' failed on key '{_mask_key(key)}': {e}. Trying next key...")
                    continue        # next key, same model

        # All keys × all models exhausted for this attempt
        if attempt < 2:
            is_rate_limit = (
                isinstance(last_err, urllib.error.HTTPError)
                and last_err.code in (429, 503)
            )
            if is_rate_limit:
                if getattr(last_err, "code", None) == 503:
                    wait_time = 10
                    print(
                        f"[!] Gemini models temporarily overloaded (HTTP 503). "
                        f"Waiting {wait_time}s before retrying (attempt {attempt + 1}/3)..."
                    )
                else:
                    wait_time = 30 if getattr(last_err, "is_quota", False) else _RPM_WAIT_SEC
                    print(
                        f"[!] All API keys hit 429 Rate Limit! "
                        f"Waiting {wait_time}s for quota window to reset "
                        f"(attempt {attempt + 1}/3)..."
                    )
                _interruptible_sleep(wait_time)
            else:
                break   # non-transient error — stop retrying

    raise last_err or Exception("All Gemini API keys and models failed.")


# ─────────────────────────────────────────────────────────────────────────────
# Vision (multimodal) variant
# ─────────────────────────────────────────────────────────────────────────────
def call_gemini_vision(
    system_prompt: str,
    user_text: str,
    image_path: str,
    api_key: Union[str, List[str]],
    model: str = "gemini-3.5-flash-lite",
    temperature: float = 0.7,
    max_tokens: int = 2048,
) -> tuple:
    """Multimodal Gemini Vision call: sends a frame image + text for grounded narration."""
    import base64

    api_keys = _normalize_keys(api_key)
    models_to_try = _build_model_list(model)

    with open(image_path, "rb") as f:
        img_b64 = base64.b64encode(f.read()).decode("utf-8")

    import brain.config as cfg
    from brain.tracker import reserve_model_usage
    model_limits = cfg.load_config().get("gemini", {}).get("model_limits", {})

    last_err = None

    for attempt in range(3):
        for m in models_to_try:
            limit = int(model_limits.get(m, 500 if "flash" in m else 50))
            model_404 = False  # FIX-W2: track per-model 404
            for key in api_keys:
                if model_404:
                    break  # FIX-W2: model not found for any key → skip whole model
                key = str(key).strip()
                if not key:
                    continue
                
                if not reserve_model_usage(key, m, limit):
                    continue

                url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent"
                img_mime = (
                    "image/jpeg" if image_path.lower().endswith(('.jpg', '.jpeg'))
                    else "image/png" if image_path.lower().endswith('.png')
                    else "image/webp" if image_path.lower().endswith('.webp')
                    else "image/jpeg"
                )
                payload = {
                    "system_instruction": {"parts": [{"text": system_prompt}]},
                    "contents": [{
                        "role": "user",
                        "parts": [
                            {"inlineData": {"mimeType": img_mime, "data": img_b64}},
                            {"text": user_text},
                        ],
                    }],
                    "generationConfig": {
                        "temperature": temperature,
                        "maxOutputTokens": max_tokens,
                        "responseMimeType": "application/json",
                    },
                }

                try:
                    req = urllib.request.Request(
                        url,
                        data=json.dumps(payload).encode("utf-8"),
                        headers={"Content-Type": "application/json", "x-goog-api-key": key},
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=45.0) as response:
                        res_data = json.loads(response.read().decode("utf-8"))
                        _record_api_usage(key, m, "success")
                        text = _extract_text_from_gemini_response(res_data)
                        return text, m

                except urllib.error.HTTPError as e:
                    last_err = e
                    err_code = e.code
                    err_body = ""
                    try: err_body = e.read().decode("utf-8")
                    except Exception as _exc:
                        log_suppressed("brain.gemini_client.call_gemini_vision", _exc)

                    if err_code == 429:
                        setattr(e, "is_quota", "quota" in err_body.lower() or "exhausted" in err_body.lower())
                        if getattr(e, "is_quota", False):
                            print(f"[WARN] Vision Model '{m}' Daily Quota Exhausted (429) on key '{_mask_key(key)}'. Retrying next key...")
                        else:
                            print(f"[WARN] Vision Model '{m}' rate-limited (429). Retrying next key...")
                        _record_api_usage(key, m, "rate_limited")
                        _interruptible_sleep(1)
                        continue
                    elif err_code in (404, 503):
                        # FIX-W2: 404/503 = model not available for any key → skip whole model
                        print(f"[WARN] Vision Model '{m}' returned HTTP {err_code}. Skipping to next model...")
                        _record_api_usage(key, m, f"error_{err_code}")
                        model_404 = True
                        break           # FIX-W2: break key loop → go to next model
                    else:
                        print(f"[WARN] Vision Model '{m}' returned HTTP {err_code}: {err_body[:200]} -- trying next key...")
                        _record_api_usage(key, m, f"error_{err_code}")
                        continue

                except (TimeoutError, urllib.error.URLError) as e:
                    last_err = e
                    _record_api_usage(key, m, "timeout")
                    print(f"[WARN] Vision Model '{m}' timed out or connection error on key '{_mask_key(key)}': {e}. Trying next key...")
                    continue

                except Exception as e:
                    last_err = e
                    print(f"[WARN] Vision Model '{m}' failed on key '{_mask_key(key)}': {e}. Trying next key...")
                    continue

        if attempt < 2:
            is_rate_limit = (
                isinstance(last_err, urllib.error.HTTPError)
                and last_err.code == 429
                and not getattr(last_err, "is_quota", False)
            )
            if is_rate_limit:
                print(
                    f"[!] VisionAPI: All keys rate-limited. "
                    f"Waiting {_RPM_WAIT_SEC}s (attempt {attempt + 1}/3)..."
                )
                _interruptible_sleep(_RPM_WAIT_SEC)
            else:
                break

    raise last_err or Exception("All Gemini Vision API calls failed.")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def _normalize_keys(api_key: Union[str, List[str]]) -> List[str]:
    if isinstance(api_key, str):
        raw = api_key.replace("\r\n", ",").replace("\n", ",").replace(";", ",")
        return [k.strip() for k in raw.split(",") if k.strip()]
    elif isinstance(api_key, (list, tuple)):
        result = []
        for item in api_key:
            if isinstance(item, str):
                raw = item.replace("\r\n", ",").replace("\n", ",").replace(";", ",")
                for sub in raw.split(","):
                    sub_clean = sub.strip()
                    if sub_clean:
                        result.append(sub_clean)
            elif item:
                val = str(item).strip()
                if val:
                    result.append(val)
        return result
    return []


def _build_model_list(requested_model: str) -> List[str]:
    """De-duplicated list: requested model first (defaulting to gemini-3.5-flash-lite), then standard fallbacks."""
    seen = set()
    result = []
    primary = requested_model.strip() if (isinstance(requested_model, str) and requested_model.strip()) else "gemini-3.5-flash-lite"
    for m in [primary] + _FALLBACK_MODELS:
        if m and m not in seen:
            seen.add(m)
            result.append(m)
    return result


def _record_api_usage(key: str, model: str, status: str):
    """Tracks API usage per key using brain.tracker (Google Official UTC Reset Time)."""
    try:
        from brain.tracker import record_key_usage
        record_key_usage(key, model, status)
    except Exception as e:
        print(f"[WARN] Failed to record API usage: {e}")

# ------------------------------------------------------------------------------------------------
# ------------------------------------------------------------------------------------------------
# Video File API (For Hybrid Architecture)
# ------------------------------------------------------------------------------------------------
def upload_video_file(video_path: str, api_key) -> tuple:
    api_keys = _normalize_keys(api_key)
    file_size = os.path.getsize(video_path)
    mime_type = "video/mp4"
    
    for key in api_keys:
        start_url = "https://generativelanguage.googleapis.com/upload/v1beta/files"
        start_headers = {
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(file_size),
            "X-Goog-Upload-Header-Content-Type": mime_type,
            "Content-Type": "application/json",
            "x-goog-api-key": key
        }
        start_payload = json.dumps({"file": {"display_name": os.path.basename(video_path)}}).encode("utf-8")
        
        upload_url = None
        for start_attempt in range(3):
            try:
                req1 = urllib.request.Request(start_url, data=start_payload, headers=start_headers, method="POST")
                with urllib.request.urlopen(req1, timeout=30) as res1:
                    upload_url = res1.headers.get("X-Goog-Upload-URL")
                if upload_url:
                    break
            except Exception as e:
                print(f"[WARN] Upload start attempt {start_attempt+1}/3 failed for key {_mask_key(key)}: {e}")
                if start_attempt < 2:
                    time.sleep(2 * (start_attempt + 1))
            
        if not upload_url:
            continue
            
        print(f"[*] Gemini API: Uploading {os.path.basename(video_path)} via REST (Streaming to prevent memory exhaustion)...")
        upload_headers = {
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "upload, finalize",
            "X-Goog-Upload-Offset": "0",
            "Content-Length": str(file_size)
        }
        
        file_name = None
        for upload_attempt in range(3):
            try:
                with open(video_path, "rb") as f:
                    req2 = urllib.request.Request(upload_url, data=f, headers=upload_headers, method="POST")
                    with urllib.request.urlopen(req2, timeout=600) as res2:
                        res_data = json.loads(res2.read().decode("utf-8"))
                        file_name = res_data.get("file", {}).get("name")
                if file_name:
                    break
            except Exception as e:
                print(f"[WARN] Upload streaming attempt {upload_attempt+1}/3 failed: {e}")
                if upload_attempt < 2:
                    time.sleep(3 * (upload_attempt + 1))
            
        if not file_name:
            continue
            
        print(f"[*] Gemini API: Waiting for video processing ({file_name})...")
        max_poll_attempts = 120  # Max 10 minutes (120 * 5s)
        for _ in range(max_poll_attempts):
            time.sleep(5)
            check_url = f"https://generativelanguage.googleapis.com/v1beta/{file_name}"
            try:
                req_check = urllib.request.Request(check_url, headers={"x-goog-api-key": key}, method="GET")
                with urllib.request.urlopen(req_check, timeout=30) as r:
                    info = json.loads(r.read().decode("utf-8"))
                    state = info.get("state")
                    if state == "ACTIVE":
                        print(f"[OK] Gemini API: Video '{file_name}' is ACTIVE and ready!")
                        return file_name, key
                    elif state == "FAILED":
                        # BUG-L5 Fix: break here instead of raising — the raise was caught by
                        # 'except Exception' below and caused it to keep polling for 10 more minutes.
                        print(f"[WARN] Video processing FAILED on Google servers for {file_name}. Trying next API key.")
                        break
            except Exception as e:
                print(f"[WARN] Check status attempt failed (will retry): {e}")
                
    raise Exception("All Gemini API keys failed to upload the video.")

def ask_gemini_with_video(file_name: str, system_prompt: str, user_text: str, key: str, model: str = "gemini-3.5-flash-lite", temperature: float = 0.3) -> str:
    models_to_try = _build_model_list(model)
    last_err = None
    
    for m in models_to_try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent"
        payload = {
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": [{
                "role": "user",
                "parts": [
                    {"fileData": {"mimeType": "video/mp4", "fileUri": f"https://generativelanguage.googleapis.com/v1beta/{file_name}"}},
                    {"text": user_text},
                ],
            }],
            "generationConfig": {
                "temperature": temperature,
                "responseMimeType": "application/json",
            },
        }
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json", "x-goog-api-key": key}, method="POST")
        print(f"[*] Gemini API: Sending prompt and video '{file_name}' to {m}...")
        
        max_retries = 3
        for attempt in range(max_retries):
            try:
                with urllib.request.urlopen(req, timeout=600.0) as response:
                    res_data = json.loads(response.read().decode("utf-8"))
                    return _extract_text_from_gemini_response(res_data)
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code in [503, 500, 429]:
                    if attempt < max_retries - 1:
                        wait_sec = (attempt + 1) * 10
                        print(f"[WARN] {m} returned {e.code}. Retrying in {wait_sec}s...")
                        _interruptible_sleep(wait_sec)
                        continue
                    else:
                        print(f"[WARN] {m} failed after {max_retries} attempts with HTTP {e.code}. Falling back to next model...")
                        break # break inner retry loop, continue to next model
                else:
                    print(f"[WARN] {m} returned HTTP {e.code}. Falling back to next model...")
                    break
            except Exception as e:
                last_err = e
                print(f"[WARN] {m} failed: {e}. Falling back to next model...")
                break
                
    raise Exception(f"All models failed for Video API. Last error: {last_err}")

def delete_video_file(file_name: str, key: str):
    url = f"https://generativelanguage.googleapis.com/v1beta/{file_name}"
    req = urllib.request.Request(url, headers={"x-goog-api-key": key}, method="DELETE")
    try:
        urllib.request.urlopen(req, timeout=30)
        print(f"[OK] Gemini API: Deleted video '{file_name}'.")
    except Exception as e:
        print(f"[WARN] Failed to delete video file {file_name}: {e}")
