"""
Gemini text-to-speech: a second narrator voice for the pipeline.

Edge TTS is free and fast, but it is one service owned by one company; when Microsoft refuses the
connection (HTTP 403 for an outdated client, an outage, a blocked network) every video would be silent.
Gemini speaks Burmese too and the studio already holds the user's Gemini keys, so it serves both as a
voice the user can choose (``gemini:Charon``, ``gemini:Kore`` ...) and as the automatic stand-in when Edge
is down (``VoiceAgent._switch_to_fallback``).

Design notes
  * Plain REST through ``urllib`` like ``brain/gemini_client.py`` - no extra dependency.
  * Keys rotate (round-robin, so the free per-minute limit is spread over all of them); a key whose daily
    quota is used up is skipped for the rest of the run; a model that does not exist is skipped.
  * A (key, model) that hit its per-minute limit cools down for 45 s and the line waits exactly until one is ready,
    instead of knocking again; a model that times out is not asked again with the other keys (that took minutes per
    line), it is tried last from then on. Every such decision is said once in the log.
  * The narration is sent as plain text. A spoken-style prefix such as "Say in a calm voice: ..." is READ ALOUD by
    the current models (measured on the real service), so a style is sent only when the user configures one, in
    the documented director's-notes layout. The speed the job asked for is applied afterwards with ffmpeg ``atempo``
    because the service has no speed parameter.
  * The service answers raw 16-bit PCM; ``pcm_to_mp3`` makes the ``scene_XXXX.mp3`` the rest of the
    pipeline expects.

Check it with your keys:   python -m agents.gemini_tts --check

Gemini ဖြင့် အသံထုတ်ခြင်း - Edge TTS မရလျှင် အစားထိုးနိုင်ပြီး voice အဖြစ်လည်း ရွေးသုံးနိုင်သည်။
"""
import base64
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from typing import Callable, Iterable, List, Optional, Sequence, Tuple

from brain.diagnostics import log_suppressed

API_BASE = "https://generativelanguage.googleapis.com/v1beta"
VOICE_PREFIX = "gemini:"

# The 30 prebuilt voices of the service. Any of them can be named: ``gemini:Puck``.
VOICES = (
    "Zephyr", "Puck", "Charon", "Kore", "Fenrir", "Leda", "Orus", "Aoede", "Callirrhoe", "Autonoe",
    "Enceladus", "Iapetus", "Umbriel", "Algieba", "Despina", "Erinome", "Algenib", "Rasalgethi", "Laomedeia",
    "Achernar", "Alnilam", "Schedar", "Gacrux", "Pulcherrima", "Achird", "Zubenelgenubi", "Vindemiatrix",
    "Sadachbia", "Sadaltager", "Sulafat",
)
DEFAULT_MALE, DEFAULT_FEMALE = "Charon", "Kore"
_FEMALE_EDGE_NAMES = ("nilar", "jenny", "aria", "xiaoxian")

# The UI / CLI language shortcuts that select a Gemini narrator (same idea as burmese_thiha / burmese_nilar).
SHORTCUTS = {
    "burmese_gemini_male": VOICE_PREFIX + DEFAULT_MALE,
    "burmese_gemini_female": VOICE_PREFIX + DEFAULT_FEMALE,
}

# Tried in this order when neither config.json ("voice.gemini_tts.models") nor GEMINI_TTS_MODEL names one
# and the account lists none. A name the service does not know is skipped, so a stale entry costs one request.
DEFAULT_MODELS = ("gemini-2.5-flash-preview-tts", "gemini-2.5-pro-preview-tts")

SAMPLE_RATE = 24000                    # the service speaks 24 kHz mono 16-bit PCM
REQUEST_TIMEOUT_SEC = 90
MAX_ROUNDS = 3                         # passes over every key x model before giving up on transient trouble
MAX_WAITS = 4                          # times one line waits for a per-minute limit to lift before giving up
RATE_COOLDOWN_SEC = 45                 # a (key, model) that hit its per-minute limit is left alone this long
_WAIT_SEC = {"overloaded": 10, "network": 3}


class GeminiTTSError(RuntimeError):
    """Why a line could not be voiced. ``retryable`` is False when asking again cannot help (quota, region, keys)."""

    def __init__(self, message: str, kind: str = "unavailable", retryable: bool = True):
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable


# ─────────────────────────────────────────────────────────────────────────────
# Voice names
# ─────────────────────────────────────────────────────────────────────────────
def is_gemini_voice(voice) -> bool:
    return str(voice or "").strip().lower().startswith(VOICE_PREFIX)


def normalize_voice(voice) -> Optional[str]:
    """``"gemini:kore"`` -> ``"gemini:Kore"``; None when it is not a Gemini voice or the name is unknown."""
    text = str(voice or "").strip()
    if not text.lower().startswith(VOICE_PREFIX):
        return None
    wanted = text[len(VOICE_PREFIX):].strip().lower()
    for name in VOICES:
        if name.lower() == wanted:
            return VOICE_PREFIX + name
    return None


def canonical_voice(voice):
    """A Gemini voice spelled canonically (``gemini:kore`` -> ``gemini:Kore``); anything else comes back unchanged."""
    return normalize_voice(voice) or voice


def voice_name(voice) -> str:
    """The service's own name of a ``gemini:`` voice (``"Kore"``); ValueError for anything else."""
    normalized = normalize_voice(voice)
    if not normalized:
        raise ValueError(f"'{voice}' is not a Gemini voice. Use gemini:<name>, for example gemini:Charon or gemini:Kore "
                         f"(names: {', '.join(VOICES)}).")
    return normalized[len(VOICE_PREFIX):]


def fallback_voice_for(edge_voice) -> str:
    """The Gemini narrator that stands in for an Edge voice: a female name -> Kore, otherwise Charon."""
    low = str(edge_voice or "").lower()
    return VOICE_PREFIX + (DEFAULT_FEMALE if any(name in low for name in _FEMALE_EDGE_NAMES) else DEFAULT_MALE)


# ─────────────────────────────────────────────────────────────────────────────
# Request / response
# ─────────────────────────────────────────────────────────────────────────────
def tempo_for(rate) -> float:
    """The job's speed choice ("+18%", 18, "-10%") as an ffmpeg ``atempo`` factor; 1.0 (no change) for None or a tiny change."""
    match = re.search(r"-?\d+", str(rate if rate is not None else ""))
    if not match or abs(int(match.group(0))) < 3:
        return 1.0
    return max(0.7, min(1.4, 1 + int(match.group(0)) / 100))


def build_prompt(text: str, style: Optional[str] = None) -> str:
    """The narration itself; with a configured style, the same director's notes before every line (one narrator all film long)."""
    manner = (style or "").strip()
    if not manner:
        return text
    return f"### DIRECTOR'S NOTES\nStyle: {manner}\n\n#### TRANSCRIPT\n{text}"


def build_request(prompt: str, voice: str) -> dict:
    return {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice_name(voice)}}},
        },
    }


def audio_from_response(data: dict) -> Tuple[bytes, int, str]:
    """(audio bytes, sample rate, mime type) of the first audio part; GeminiTTSError when there is none."""
    candidates = (data or {}).get("candidates") or []
    if not candidates:
        reason = ((data or {}).get("promptFeedback") or {}).get("blockReason", "NO_CANDIDATES")
        raise GeminiTTSError(f"Gemini returned no audio ({reason}).", kind="no_audio")
    for part in (candidates[0].get("content") or {}).get("parts") or []:
        inline = part.get("inlineData") or part.get("inline_data") or {}
        if inline.get("data"):
            mime = str(inline.get("mimeType") or inline.get("mime_type") or "audio/L16")
            found = re.search(r"rate=(\d+)", mime)
            return base64.b64decode(inline["data"]), int(found.group(1)) if found else SAMPLE_RATE, mime
    raise GeminiTTSError(f"Gemini answered without audio (finishReason={candidates[0].get('finishReason', 'unknown')}).",
                         kind="no_audio")


def pcm_to_mp3(audio: bytes, rate: int, out_path: str, ffmpeg: str = "ffmpeg", mime: str = "audio/L16",
               tempo: float = 1.0) -> None:
    """Encodes the service's raw PCM (or any other audio it sends) as an mp3 at ``out_path`` (atomic), optionally faster/slower."""
    raw = "l16" in mime.lower() or "pcm" in mime.lower()
    if len(audio) < (rate * 2 if raw else 800) // 10:   # less than a tenth of a second of sound
        raise GeminiTTSError("Gemini returned an empty audio clip.", kind="no_audio")
    part = out_path + ".part"
    source = ["-f", "s16le", "-ar", str(rate), "-ac", "1"] if raw else []
    speed = ["-filter:a", f"atempo={tempo:.3f}"] if abs(tempo - 1.0) > 0.02 else []
    cmd = [ffmpeg, "-y", "-loglevel", "error", *source, "-i", "pipe:0", *speed, "-codec:a", "libmp3lame", "-q:a", "3", "-f", "mp3", part]
    result = subprocess.run(cmd, input=audio, capture_output=True)
    if result.returncode != 0 or not os.path.exists(part) or os.path.getsize(part) < 800:
        detail = result.stderr.decode("utf-8", "replace").strip()[:200]
        try:
            os.remove(part)
        except OSError:
            pass
        raise GeminiTTSError(f"ffmpeg could not encode the Gemini audio: {detail or 'no output'}", kind="encode")
    os.replace(part, out_path)


def _classify(code: int, body: str) -> str:
    """Names an HTTP failure: quota (used up today), rate (per minute), key, region, model, overloaded or rejected."""
    low = (body or "").lower()
    if code == 429:
        daily = any(word in low for word in ("perday", "per day", "daily", "free_tier_requests"))
        return "quota" if daily else "rate"
    if "location is not supported" in low or "not available in your country" in low:
        return "region"
    if code in (401, 403) or "api key not valid" in low or "api_key_invalid" in low or "expired" in low:
        return "key"
    if code == 404:
        return "model"
    if code >= 500:
        return "overloaded"
    return "rejected"


def _message_of(body: str) -> str:
    try:
        return str(json.loads(body).get("error", {}).get("message", ""))[:200]
    except Exception:
        return " ".join((body or "").split())[:200]


def list_tts_models(key: str, timeout: float = 20.0) -> List[str]:
    """The text-to-speech models this key can use (names without ``models/``), newest and cheapest first."""
    request = urllib.request.Request(f"{API_BASE}/models?pageSize=1000", headers={"x-goog-api-key": key})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        listing = json.loads(response.read().decode("utf-8"))
    names = [m.get("name", "").split("/", 1)[-1] for m in listing.get("models", [])
             if "tts" in m.get("name", "").lower() and "generateContent" in (m.get("supportedGenerationMethods") or ["generateContent"])]

    def order(name: str):
        version = [int(n) for n in re.findall(r"\d+", name)]
        return (-(version[0] if version else 0), -(version[1] if len(version) > 1 else 0), "pro" in name, "lite" in name, name)

    return sorted(set(names), key=order)


def configured_models(config: Optional[dict] = None) -> List[str]:
    """Models named by GEMINI_TTS_MODEL or config.json (``voice.gemini_tts.models``), else the defaults."""
    named = os.getenv("GEMINI_TTS_MODEL") or ((config or {}).get("models"))
    if isinstance(named, str):
        named = [m for m in re.split(r"[,\s;]+", named) if m]
    models = [str(m).strip() for m in (named or []) if str(m).strip()]
    return models or list(DEFAULT_MODELS)


def resolve_models(keys: Iterable[str], config: Optional[dict] = None) -> List[str]:
    """
    The models to try: the ones the user named, else the ones the account lists (asking costs no generation quota),
    else the defaults - so a renamed or retired model does not need a code change.
    """
    if os.getenv("GEMINI_TTS_MODEL") or (config or {}).get("models"):
        return configured_models(config)
    for key in keys:
        try:
            found = list_tts_models(key)
        except Exception:
            continue
        if found:
            return found
    return list(DEFAULT_MODELS)


# ─────────────────────────────────────────────────────────────────────────────
# The synthesizer
# ─────────────────────────────────────────────────────────────────────────────
class GeminiSynthesizer:
    """Voices one line at a time with key rotation and model fallback. Safe to call from several threads."""

    def __init__(self, api_keys: Iterable[str], models: Optional[Sequence[str]] = None, style: Optional[str] = None,
                 ffmpeg: str = "ffmpeg", timeout: float = REQUEST_TIMEOUT_SEC, log: Optional[Callable[[str], None]] = None):
        from brain.gemini_client import _normalize_keys
        self.keys = _normalize_keys(list(api_keys or []))
        self.models = list(models or DEFAULT_MODELS)
        self.style = style
        self.ffmpeg = ffmpeg
        self.timeout = timeout
        self._log = log or print
        self._clock = time.monotonic
        self._lock = threading.Lock()
        self._cursor = 0
        self._used_up = set()          # (key index, model): daily quota gone
        self._bad_keys = set()         # key indexes the service refuses
        self._dead_models = set()      # models the service does not know
        self._slow_models = set()      # models that timed out: tried last
        self._cooldown = {}            # (key index, model) -> when its per-minute limit should be over
        self._said = set()

    # -- bookkeeping ----------------------------------------------------------
    def _say(self, once_key, message: str) -> None:
        """Tells the user something once per run (the lines of a film would repeat it a hundred times)."""
        with self._lock:
            if once_key in self._said:
                return
            self._said.add(once_key)
        self._log(message)

    def _candidates(self) -> Tuple[List[Tuple[int, str]], Optional[float]]:
        """
        ``(ready, wait)``: every (key index, model) worth trying now, starting at the next key so the load spreads over
        all keys, models that timed out last; and, when nothing is ready only because of per-minute limits, how many
        seconds until the first one is (None when there is no such case).
        """
        with self._lock:
            start = self._cursor
            self._cursor += 1
            now = self._clock()
            models = ([m for m in self.models if m not in self._slow_models] + [m for m in self.models if m in self._slow_models])
            count = max(len(self.keys), 1)
            ready, cooling = [], []
            for model in models:
                if model in self._dead_models:
                    continue
                for index in sorted(range(len(self.keys)), key=lambda k: (k - start) % count):
                    if index in self._bad_keys or (index, model) in self._used_up:
                        continue
                    until = self._cooldown.get((index, model), 0.0)
                    if until > now:
                        cooling.append(until - now)
                    else:
                        ready.append((index, model))
        return ready, (min(cooling) if cooling and not ready else None)

    def _mark(self, kind: str, index: int, model: str) -> None:
        with self._lock:
            if kind == "quota":
                self._used_up.add((index, model))
            elif kind == "key":
                self._bad_keys.add(index)
            elif kind == "model":
                self._dead_models.add(model)
            elif kind == "rate":
                self._cooldown[(index, model)] = self._clock() + RATE_COOLDOWN_SEC
            elif kind == "network":
                self._slow_models.add(model)
        if kind == "quota":
            self._say(("quota", index, model), f"[WARN] Gemini TTS: key #{index + 1} has no daily quota left for {model}.")
        elif kind == "key":
            self._say(("key", index), f"[WARN] Gemini TTS: key #{index + 1} is refused by the service; it is not used again.")
        elif kind == "model":
            self._say(("model", model), f"[WARN] Gemini TTS: the model {model} is not available to these keys; skipped.")
        elif kind == "network":
            self._say(("slow", model), f"[WARN] Gemini TTS: {model} did not answer in time; it is tried last from now on.")

    @staticmethod
    def _record(key: str, model: str, status: str) -> None:
        try:
            from brain.gemini_client import _record_api_usage
            _record_api_usage(key, model, status)
        except Exception as exc:                       # the usage statistics must never break a narration
            log_suppressed("agents.gemini_tts._record", exc)

    # -- one request ----------------------------------------------------------
    def _request(self, key: str, model: str, payload: dict) -> dict:
        request = urllib.request.Request(
            f"{API_BASE}/models/{model}:generateContent", data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "x-goog-api-key": key}, method="POST")
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    # -- the public call ------------------------------------------------------
    def synthesize(self, text: str, voice: str, output_path: str, rate=None) -> str:
        """Voices ``text`` into the mp3 ``output_path``; returns the model that did it. Raises GeminiTTSError."""
        from brain.gemini_client import _interruptible_sleep
        if not self.keys:
            raise GeminiTTSError("Gemini text-to-speech needs a Gemini API key (add one in Settings or set GEMINI_API_KEYS).",
                                 kind="key", retryable=False)
        payload = build_request(build_prompt(text, self.style), voice)
        tempo = tempo_for(rate)
        last = ""
        passes = waits = 0
        while passes < MAX_ROUNDS:
            ready, wait = self._candidates()
            if not ready and wait is not None:                      # every usable key is cooling down after its per-minute limit
                if waits >= MAX_WAITS:
                    break
                waits += 1
                pause = max(1.0, wait)
                self._say(("wait", waits, int(self._clock() // 30)),
                          f"[*] Gemini TTS: every key hit its per-minute limit; waiting {pause:.0f}s for one to be free...")
                _interruptible_sleep(pause)
                continue
            if not ready:
                break
            passes += 1
            transient, limited = None, False
            timed_out = set()                                       # a model that timed out is not asked again with another key
            for index, model in ready:
                if model in timed_out or model in self._dead_models or index in self._bad_keys or (index, model) in self._used_up:
                    continue                                       # found out a moment ago, by another key or another line
                if self._cooldown.get((index, model), 0.0) > self._clock():
                    continue
                key = self.keys[index]
                try:
                    audio, sample_rate, mime = audio_from_response(self._request(key, model, payload))
                    pcm_to_mp3(audio, sample_rate, output_path, self.ffmpeg, mime, tempo)
                    self._record(key, model, "success")
                    return model
                except GeminiTTSError as exc:                      # answered, but without usable audio
                    last = str(exc)
                    self._record(key, model, "error")
                    continue
                except urllib.error.HTTPError as exc:
                    try:
                        body = exc.read().decode("utf-8", "replace")
                    except Exception:
                        body = ""
                    kind = _classify(exc.code, body)
                    last = f"HTTP {exc.code}: {_message_of(body)}"
                    self._record(key, model, "rate_limited" if kind in ("quota", "rate") else f"error_{exc.code}")
                except (TimeoutError, urllib.error.URLError, OSError) as exc:
                    kind, last = "network", f"{type(exc).__name__}: {exc}"
                    self._record(key, model, "timeout")
                    timed_out.add(model)
                self._mark(kind, index, model)
                if kind == "region":
                    raise GeminiTTSError("Gemini does not serve this location. Use a VPN (or a network in a supported "
                                         f"country) and try again. ({last})", kind="region", retryable=False)
                if kind == "rate":
                    limited = True
                elif kind in _WAIT_SEC and transient is None:
                    transient = kind
            if transient is not None:
                _interruptible_sleep(_WAIT_SEC[transient])
            elif not limited:
                break                                              # nothing transient happened: asking again cannot help
        raise self._final_error(set(), last)

    def _final_error(self, seen: set, last: str) -> GeminiTTSError:
        with self._lock:
            usable = [(i, m) for m in self.models if m not in self._dead_models for i in range(len(self.keys))
                      if i not in self._bad_keys and (i, m) not in self._used_up]
        if not usable and self._used_up:
            return GeminiTTSError("Gemini text-to-speech: the daily quota of every key is used up. The lines already voiced "
                                  "are kept - run again tomorrow, add another key, or choose an Edge voice.",
                                  kind="quota", retryable=False)
        if not usable and self._bad_keys and len(self._bad_keys) >= len(self.keys):
            return GeminiTTSError(f"Gemini refused every API key ({last}). Check the keys in Settings.", kind="key", retryable=False)
        if not usable and self._dead_models and len(self._dead_models) >= len(self.models):
            return GeminiTTSError("None of the Gemini text-to-speech models is available to these keys "
                                  f"({', '.join(self.models)}). Set GEMINI_TTS_MODEL to one that is "
                                  "(python -m agents.gemini_tts --check lists them).", kind="model", retryable=False)
        return GeminiTTSError(f"Gemini text-to-speech did not answer ({last or 'no details'}).", kind="unavailable")


# ─────────────────────────────────────────────────────────────────────────────
# Command line: check that the keys can speak
# ─────────────────────────────────────────────────────────────────────────────
def _main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse
    import tempfile

    import brain.config as cfg
    from agents.voice_agent import VoiceAgent

    parser = argparse.ArgumentParser(description="Check Gemini text-to-speech with the configured API keys.")
    parser.add_argument("--check", action="store_true", help="list the TTS models and voice one short sentence")
    parser.add_argument("--voice", default=VOICE_PREFIX + DEFAULT_FEMALE, help="gemini:<name> (default %(default)s)")
    parser.add_argument("--text", default="မင်္ဂလာပါ။")
    parser.add_argument("--style", help="director's notes to try, for example: a calm, warm storyteller (default: none)")
    parser.add_argument("--out", help="where to save the mp3 (default: a temporary file)")
    args = parser.parse_args(argv)

    config = cfg.load_config()
    keys = list(config.get("gemini", {}).get("api_keys", []))
    settings = config.get("voice", {}).get("gemini_tts", {})
    print(f"Gemini API keys found: {len(keys)}")
    if not keys:
        print("Add a key in Settings or set GEMINI_API_KEYS, then run this again.")
        return 2
    try:
        discovered = list_tts_models(keys[0])
        print("TTS models for the first key:", ", ".join(discovered) or "(none listed)")
    except Exception as exc:
        discovered = []
        print(f"Could not list the models: {type(exc).__name__}")
    models = configured_models(settings) if (os.getenv("GEMINI_TTS_MODEL") or settings.get("models")) else (discovered or list(DEFAULT_MODELS))
    print("Models tried, in order:", ", ".join(models))
    out = args.out or os.path.join(tempfile.gettempdir(), "pai_gemini_tts_check.mp3")
    try:
        used = GeminiSynthesizer(keys, models, args.style or settings.get("style"), VoiceAgent._ffmpeg_exe()).synthesize(args.text, args.voice, out)
    except GeminiTTSError as exc:
        print(f"FAILED ({exc.kind}): {exc}")
        return 1
    print(f"OK: {args.voice} spoke with {used} -> {out} ({os.path.getsize(out)} bytes)")
    print("Listen to it once: it must say only the sentence. A clip several seconds longer than the sentence needs means the "
          "model read the director's notes aloud - remove the style.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
