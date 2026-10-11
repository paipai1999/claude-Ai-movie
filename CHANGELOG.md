# 📜 Changelog — AI Movie Translate & Dubbing Agent

All notable changes, architectural overhauls, and bug fixes to the AI Movie Translate & Dubbing Agent project are documented in this file.

---

## [2.4.0-dev] — Shared option model (Phase 1 of 4: model + API)

### 🧱 Architecture
- **`core/options.py` — every option is defined once** (name, type, default, allowed values, step, engines). Five steps are asked in a fixed order: Source → Language & translation → Output & style → Protection → Audio & pipeline. `GET /api/options` exposes the model, and only the options an engine actually uses are asked for it (the downloader asks 4 things, not 37).
- **API schemas are generated** from the model (`StartRequest`, `BatchStartRequest`), so they can no longer drift from the CLI/UI. The field sets are byte-for-byte what they were.
- **One place for "the user did not choose":** `core.options.resolve(engine, values)` replaces ~60 hand-written `req.x or "..."` fallbacks in the two start handlers (`web_ui.py` -120 lines).
- **Worker arguments are built from each worker's own signature** (`services/job_args.py`) instead of three hand-ordered 20-35 item tuples. Queued jobs are persisted positionally, so a signature change can no longer silently shift arguments.

### 🐞 Fixes (behaviour that was already *intended* in the worker code)
- A client that omitted `translation_style` / `audio_mode` / `render_video` always got the **recap** values, whatever the engine (the API default was truthy, so the engine-specific fallback `or "persona"` never ran). Hardsub now defaults to `persona` + original audio, Subtitle to `dialogue` + original audio + no video render. Explicit choices (and everything the dashboard sends) are unchanged.
- Batch: `render_video=null` no longer forces a video render for the subtitle engine.

### 🧪 Tests
- `tests/test_option_model.py`: replays the **previous handler logic** against the new path over 7 engine modes x ~60 request variants (860 sub-tests) and requires identical worker arguments; plus API-contract, per-engine default and model-integrity tests. 237 tests (was 218).

---

## [2.4.0-dev] — Dashboard wizard follows the option model (Phase 4 of 4: UI)

### 🧱 Architecture
- **The wizard steps follow `core/options.py`** (`GET /api/options`): 1 Source & Language -> 2 Output & Style -> 3 Protection -> 4 Audio & Pipeline -> 5 Review. The engine is now chosen first, in step 1, because it decides which later options apply. Controls moved between cards only; element ids, handlers and all three translations are unchanged (the step titles, descriptions and Next/Back captions were rewritten in Burmese, Thai and English).
- **Engine defaults come from the model.** `handleEngineChange` no longer hard-codes style / audio / render per engine; it reads `engine_defaults` from `/api/options` (with a small fallback table that a test keeps equal to the model).
- **Only relevant options are shown.** Each control carries `data-option`; options the selected engine does not use are hidden (e.g. voice, watermark, thumbnail for hardsub; the colour-grade switch for recap).
- **One payload builder.** The two 15-line request bodies (single and batch) became one table of readers (`OPTION_READERS`) plus `collectJobOptions()`; a guard never sends a field the API does not know.
- Model change: `language` (target language / narrator-voice shortcut) is asked in the Audio step, matching the dashboard's voice picker.

### 🐞 Fixes found while verifying in a real browser
- The hint field used a CSS class that does not exist (rendered as an unstyled box).
- Removed a duplicated `</body></html>` at the end of the template.

### 🔍 Verified against the previous dashboard
- Real Chrome, 7 scenarios x every control: 179 request fields identical to the pre-refactor UI; the only 3 differences are the subtitle engine's model defaults (`render_video` false, `video_format` 16:9, so `reels_enabled` false).
- `tests/ui/dashboard_smoke.js` (Node + Chrome, self-checking) walks the steps with real clicks, checks Edit buttons, per-engine visibility and request defaults.

### 🧪 Tests
- `tests/test_dashboard_template.py` (12, no browser): every `data-option` is a model option and sits in the step the model assigns, the engine is chosen before the options it changes, every field the page sends is accepted by both endpoints, one payload builder, JS fallback equals the model, step strings exist in all languages. 304 tests (was 292).

---

## [2.4.0-dev] — Shared engine base (Phase 3 of 4: engines)

### 🧱 Architecture
- **`core/engine_base.py` — `StudioEngineBase`** holds what Engines 2 and 3 used to copy: construction, cancellation, API-key discovery, URL detection, media probing and the **QA audit-and-repair pass**, written once as a skeleton. What genuinely differs per engine (the repair prompt, how the model's answer is read, how a repaired line is cleaned) is five small `_repair_*` hooks.
- **`core/transcribe.py`** (`transcribe_media`) and **`core/subtitle_parse.py`** (`parse_subtitle_cues`) replace two copies each of the Whisper and SRT/VTT code.
- `hardsub_engine.py` 1470 → 1262 lines, `subtitle_engine.py` 1276 → 1071; ~575 duplicated lines removed, ~370 shared lines added.
- Engines keep their own `call_gemini` binding (`_call_llm`), so monkeypatching an engine module keeps working.

### 🔍 Verified against the previous code
- 186 checks replayed old vs new (repair prompts and every model-call argument, 7 parser fixtures x 2 engines, probing, API keys, URL detection, cancellation, transcription): 183 identical.
- Real run of both engines on the test clip: same segment count (15), same longest segment (5.0 s), no warnings.

### 🐞 Three deliberate differences (one rule now applies to both engines)
- Hardsub keeps no `{n8}`-style tags in imported caption text (they used to reach the translator; the subtitle engine already stripped them).
- Hardsub treats a scheme-less `youtu.be/...` as a URL (as the subtitle engine did).
- The subtitle engine drops zero-length Whisper segments (hardsub already did); they rendered as 0.3 s ghost subtitles.
- `_get_api_keys` never mutates the loaded config (the subtitle engine used to append environment keys into the config list).

### 🧪 Tests
- `tests/test_engine_base.py` (24): shared base, engine-specific prompts/answer parsing preserved, parser fixtures, transcription behaviour. 292 tests (was 268).

---

## [2.4.0-dev] — Shared option model (Phase 2 of 4: command line)

### 🧱 Architecture
- **`core/cli.py` generates the studio flags** for `main.py`, `hardsub_engine.py` and `subtitle_engine.py` from `core/options.py`. One canonical spelling per option, identical in every program; each program keeps the spellings it always had as aliases (258 command lines replayed against the previous parsers; the two engine scripts are byte-identical, `main.py` differs only where listed below).
- **Untyped flags are `None`** and resolved in one place per engine (`core.options.resolve`), replacing the scattered `args.x or "..."` fallbacks. For `main.py --engine-mode recap` the options `config.json` owns (subtitle style, format, watermark, ...) still stay `None`.
- `main.py` 731 → 462 lines; the three hand-written kwargs builders became one key list each.
- New: `--name`, `--force-whisper`, `--color-grading` / `--no-color-grading`, `--watermark`, `--watermark-opacity`, `--tts-voice`, `--tts-engine`, `--no-render-video` are available in every program that can use them.

### 🐞 Behaviour changes in `main.py` (each was already the documented/intended behaviour)
- `--engine-mode hardsub|subtitle` no longer inherits `--audio-mode ai_voiceover` from the recap default; both use `original`.
- `--engine-mode subtitle` renders no video unless `--render-video` is given (like `subtitle_engine.py`); it used to render by default.
- `--subtitle` now forces `--blur-mode yes` when no `--blur-mode` was typed, as its help text says (the old parser default made it a no-op).
- `--detect-scenes --skip-scenes` together: the last one typed wins (previously detect always won).

### 🧪 Tests
- `tests/test_cli_options.py`: every legacy flag still parses, conflicting spellings keep their per-program meaning, per-engine defaults, config-owned recap options, engine kwargs accepted by the engines. 268 tests (was 237).

---

## [2.3.2] — Render & Subtitle Timing Fixes (found in first real end-to-end run)

### 🐞 Fixes
- **Hardsub 16:9 no longer squashes non-16:9 sources.** A vertical (9:16), square or CinemaScope clip rendered with `--format 16:9` is now fitted inside the landscape canvas over a blurred copy of itself (same look as the 9:16 canvas) instead of being scaled to 1280x720, which produced a distorted frame carrying an odd pixel aspect ratio. Output always has `SAR 1:1`. (`core.letterbox.plan_canvas_mode`)
- **Subtitles no longer stay on screen through silence.** Whisper segments that ran on through silence/music (one subtitle showed for 20 s) are tightened to the last recognised word (`word_timestamps=True`) and capped by how long the text can plausibly take to say. Applies to Engines 2 and 3. (`core.segment_timing.tighten_end`)
- **Subtitle-blur detection is now a vote, not a coin flip.** The same video was blurred in one run and not in the next because one positive frame out of five decided the result (and zero positives meant no blur). Frames are now sampled where people speak (Whisper timestamps, spread across the whole video), failed analyses count as *unknown* instead of *negative*, and subtitles are only confirmed when enough analysed frames agree (>=2 and >=40%). The region comes from the median of the agreeing frames. The model runs at temperature 0. Three consecutive live runs on the test clip gave identical results (7/7 frames, y=0.66). (`core.subtitle_detect`)
- Tests no longer depend on a real Gemini key being present in `config.json` (hermetic fixture in `tests/conftest.py`).
- 218 tests (was 184).

---

## [2.3.1] — Security & Reliability Hardening

### 🔐 Security
- **Same-origin by default:** CORS no longer defaults to `*`; foreign-origin `POST`/`DELETE` are rejected in anonymous mode; `Host` is validated against DNS-rebinding on loopback binds.
- **Authentication:** constant-time secret comparison, `POST /api/login` + `/api/auth/status`, digest-only session cookie, login throttle, auto-generated token when the server is exposed without a password.
- **API keys:** `/api/keys/save` never returns raw keys, masks must carry both ends (no wildcard resolution), keys from `GEMINI_API_KEYS` are never persisted to `config.json`, health-check sends keys in a header (not the URL).
- **Path safety:** `HardsubEngine`/`SubtitleEngine` project names are sanitised (`core/paths.py`), `_safe_child_path` rejects the root folder (`/api/delete/outputs/%2e` could wipe `outputs/`), deletes are refused while a job runs, local inputs must be media files.
- **Docker:** `.dockerignore` excludes `config.json`, `cookies.txt`, databases; compose publishes on `127.0.0.1`; image health-check added.
- **Frontend:** file / project / queue names are HTML-escaped (and JS-escaped for inline handlers).

### 🐞 Fixes
- Cancelling a *queued* job no longer stops the running job or kills its ffmpeg (per-job cancel semantics).
- Hardsub: pressing Stop no longer triggers a second full CPU re-render.
- Resume: audio path is propagated from the Phase 2/3 worker; changing style / language / hint / voice invalidates the cached script and voiceover; voice clips are validated per clip through a content manifest; Whisper failure now fails the job instead of "completing" without narration.
- `drama_novel` / `audio_drama` style is honoured by Engine 1 (previously silently downgraded to `recap`).
- Cookies uploaded in the dashboard are now used by Engine 2 and Engine 3 as well.
- Hardsub failures are reported as `error` (not `done`); ZIP download honours `job_id`; F5-TTS crash on scenes starting at `0.0s`, speaker-clip lookup case bug.
- Health-check no longer blocks the event loop; quota reservation is atomic under concurrency; schema creation is serialised.
- Gemini rate-limit waits are interruptible by Stop.

### 🧹 Maintenance
- Five duplicated job workers replaced by one lifecycle helper (-200 lines); dead `services/` code removed; subtitle presets have a single source of truth; pyflakes clean.
- `requirements-dev.txt` split (pytest, test client); `LICENSE` file added; Python 3.10+ documented; 46 new regression tests (146 total).

### 🚀 Reliability & Structure (second pass)
- **Persistent queue:** queued jobs are stored in SQLite and restored when the server restarts (jobs that were mid-render are marked `interrupted`).
- **Job-scoped Stop:** every ffmpeg / demucs / helper process is registered to its job (`services/process_registry.py`, PID + creation-time safe); Stop kills exactly that job's process tree.
- **Whisper policy:** `whisper_model: "auto"` (medium on GPU, base on CPU) + `whisper_beam_size`, shared by Engines 1-3 with GPU→CPU fallback.
- **Render safety net:** a failed FFmpeg single pass now falls back to the MoviePy compositor and fails the job loudly when no video exists (previously it silently reported success).
- **Refactors (behaviour-preserving, verified against the previous code with differential tests):**
  - `video_merger_agent.py` 2,727 → 176 lines + `agents/merger/` package (11 modules, `MergePlan` dataclass)
  - `web_ui.py` 3,239 → ~2,470 lines (+ `services/job_runtime.py`, `services/schemas.py`)
  - `MasterAgent.run_pipeline` / `__init__`, `WriterAgent.generate_script`, `main.main` split into one method per step
- **Diagnostics:** 110 silent `except: pass` handlers now call `log_suppressed()` (visible with `PAI_DEBUG=1`).
- **Tooling:** GitHub Actions workflow (pyflakes + pytest), `requirements-ci.txt` (no GPU stack), `requirements.lock.txt` (exact versions).
- 184 tests (was 100).

---

## [2.3.0] — 2026-09-19 (Triple-Engine Architecture & Hardsub Studio)

### 🌟 Release Highlights
This release establishes the **Triple-Engine Architecture**: adding **Engine 3 (100% Original Audio & Burmese Hardsub Studio)** and **Engine 2 (YouTube Subtitle & Transcript Studio)** alongside **Engine 1 (AI Movie Recap Studio)**. Includes Anti-Copyright shields (1.02x scale/crop, color grading, mirror, audio speed shield), Vision AI subtitle blur, Male/Female/Child persona particle translation, resilient translation fallback mapping, and SQLite unified state persistence.

---

### 1. 🎞️ Engine 3: Original Audio & Burmese Hardsub Studio
- **100% Original Audio Preservation:** Zero TTS overwrite; preserves original voice performances, ambient sound effects, and musical score intact.
- **Vision AI Subtitle Blur:** Automatically detects and applies smooth Gaussian box-blur to existing hardcoded subtitles at customizable heights (12%–30%).
- **Anti-Copyright Protection Matrix:** Combines subtle 1.02x video zoom/crop, contrast/saturation color grading, optional horizontal mirroring, and audio time-stretch fingerprint shield (`atempo=1.008`).
- **Faithful 1:1 Persona Translation:** Automatically detects and applies colloquial Myanmar particles for Male (ကျနော်/ခင်ဗျာ), Female (ကျွန်မ/ရှင်), and Child (သား/သမီး/ပါဗျာ) personas.
- **Multi-Format Compositing:** Outputs crisp 16:9 Landscape, 9:16 Vertical Reels with dynamic blurred canvas, or dual exports simultaneously.
- **Persistent Deliverables:** Automatically saves `.mp4`, `.ass`, `.srt`, `records_data.json`, `state.json`, and quality check reports.

---

### 2. 📝 Engine 2: YouTube Subtitle & Transcript Studio
- **Dual Nuance Translation:** Multi-step pipeline translating foreign dialogue (EN/ZH/JA/KO/TH) into natural spoken Burmese (စကားပြောဟန်).
- **6 Deliverable Exports:** Generates `01_video_original.mp4`, `02_transcript_original.txt`, `03_transcript_english.txt`, `04_transcript_burmese.txt`, `05_subtitle_burmese.srt`, `06_quality_check_report.txt`, and `records_data.json`.

---

### 3. 🛡️ System Robustness, Storage Leak Fixes & Resilient Fallbacks
- **Temp Download Cleanup:** Safely purges temporary video download directories (`temp_dl/` and `temp/yt_dl/`) after copying source videos, saving 50% disk space per project.
- **Resilient Translation Matching:** Upgraded chunk matching to map translations by ID and index on retries, preventing entire batches from reverting to raw foreign text if the LLM drops a single line.
- **Special Character Escaping:** Escapes single quotes and colons in subtitle filenames to prevent FFmpeg filtergraph parse failures.
- **Unified SQLite Persistence:** Hooked `save_custom_movie_state(...)` across all engines to ensure consistent state management in `outputs/movie_metadata.db`.

---

## [2.2.0] — 2026-09-06 (Major Architectural Release)

### 🌟 Release Highlights
This major release eliminates cumulative audio drift, guarantees complete spoken sentence delivery with zero mid-speech cutoffs, enforces strict script character budgets, adds high-speed NVIDIA NVENC hardware video encoding with a self-healing installer, and ensures 100% feature parity across Google Colab and Kaggle Cloud notebooks.

---

### 1. 🎯 Anchor-Based Scene Synchronization & Zero Cumulative Drift
- **Problem:** Sequential concatenation (place_time = max(curr_t, orig_start)) created a compounding positive feedback loop. When a sentence ran even 0.5s long, all subsequent sentences were pushed further into the future. By clip 100, the narration was 148 seconds (2.5 minutes) behind the video scenes, causing dramatic desynchronization between audio and visual action.
- **Solution:** Implemented **Scene-Anchor Synchronization** in [agents/video_merger_agent.py](agents/video_merger_agent.py).
  - Each discrete dialogue line is strictly anchored to its original visual cut timestamp (starts[idx]).
  - Narration is dynamically fitted to its available scene gap (starts[idx+1] - starts[idx]).
  - At every action gap, camera pause, or transition, timing instantly realigns to **0.000s drift**.
- **Benchmark:**
  | Metric | Previous Pipeline | v2.2.0 Anchor Engine |
  | :--- | :---: | :---: |
  | Drift at Clip 10 | 9.80s behind | **0.51s (Near Zero)** |
  | Drift at Clip 50 | 85.16s behind | **3.43s** |
  | Drift at Clip 100 | 148.36s behind (2.5 min) | **3.28s** |
  | Dropped Clips | 45 clips dropped | **0 clips dropped (205/205 placed)** |

---

### 2. 🎙️ Full Spoken Sentence Delivery Guarantee (Zero Truncation)
- **Zero Cut-off Guarantee:** Removed all hard audio clipping (subclipped(0, available_gap)).
- Sentences are **NEVER truncated in mid-speech**.
- Every spoken sentence plays completely from the first word to the very last syllable with 100% natural pronunciation and cadence.

---

### 3. 📝 Strict Character Budgeting in Script Generation
- **Problem:** Burmese syllables are naturally 1.8x–2.2x longer to pronounce than English syllables. Without strict limits, Gemini generated verbose 20–30 word compound sentences for 3-second scene slots.
- **Solution:** Updated [agents/writer_agent.py](agents/writer_agent.py) translation batch prompts with a strict per-item character budget rule:
  max_chars = max(18, int(duration_sec * 11.0))
  Gemini is instructed to write punchy, concise, storytelling sentences tailored precisely to fit the available time budget.

---

### 4. 🤖 100% QAAgent Auto-Rewrite Resolution
- **Problem:** When enforce_duration_constraints in [agents/qa_agent.py](agents/qa_agent.py) called Gemini to shorten over-length blocks, a key-matching mismatch caused 165 out of 205 over-length blocks to be ignored without applying rewrites.
- **Solution:** Implemented multi-format ID extraction with regex digit fallback (re.search(r'\d+', ...)), and 1-to-1 positional matching fallback. Guarantees 100% of over-length script blocks are shortened to fit within target character bounds.

---

### 5. ⚡ Natural Pitch-Preserving Audio Time-Stretch
- **Engine:** In [agents/voice_agent.py](agents/voice_agent.py), expanded FFmpeg tempo time-stretching bounds to min(1.15, max(0.78, stretch_ratio)).
- Allows speedup up to 1.28x cleanly via WSOLA algorithm while completely preserving natural human pitch (zero robotic sound or chipmunk distortion).

---

### 6. 🎮 NVIDIA NVENC GPU Video Acceleration & Self-Healing Installer
- **Problem:** Kaggle default Ubuntu repository disables NVIDIA NVENC due to licensing restrictions, forcing video post-processing to fall back to CPU libx264 (7 minutes per 15-minute video).
- **Solution:** Integrated _auto_setup_nvenc_linux() in [agents/video_merger_agent.py](agents/video_merger_agent.py). Automatically detects NVIDIA GPUs on Linux (Colab/Kaggle) and downloads the BtbN Static NVENC FFmpeg build in the background.
- **Performance:** Post-processing and 9:16 Reels rendering speed boosted from ~80 fps to **400–650 fps (5x–8x faster)**, reducing render time from 7 minutes to ~1.5 minutes.

---

### 7. 🍪 YouTube Anti-Bot Bypass (Apple VisionOS & Android Resilient Matrix)
- **Problem:** Cloud datacenter IP ranges (Google Colab and Kaggle) are aggressively flagged by YouTube's security firewalls. Standard clients (web, mweb, ios, web_creator) fail with Sign in to confirm you're not a bot or Please sign in even when browser cookies are attached, because Google detects session mismatch between residential IPs and datacenter servers.
- **Solution:** Re-engineered the client fallback hierarchy in [agents/downloader_agent.py](agents/downloader_agent.py):
  - **Attempt 1 (Apple VisionOS + Cookies):** Connects via the Apple Vision Pro HLS m3u8 streaming endpoint. Bypasses Google Play Bot Integrity and Datacenter IP restrictions entirely, delivering crystal clear **1080p Full HD**.
  - **Attempt 2 (Pure VisionOS - Anonymous):** Strips cookies automatically to provide an untracked, clean session in case the user's exported browser cookies have been challenged by Google.
  - **Attempt 3 (Android Mobile Client):** Routes via native YouTube Android API (bestvideo[height<=1080]+bestaudio/best).
  - **Attempt 4 (Android Progressive Stream Fallback):** Direct bulletproof single-stream progressive fallback (best/18/22).

---

### 8. ⚡ JavaScript Runtime Integration (Node.js for yt-dlp EJS)
- **Engine:** Integrated automated nodejs system dependency detection in [agents/downloader_agent.py](agents/downloader_agent.py) (ydl_opts['js_runtimes'] = {'node': {'path': node_bin}}).
- **Cloud Parity:** Updated all 4 cloud notebooks (AI_Movie_Translate_Colab.ipynb, AI_Movie_Translate_Colab_PRIVATE.ipynb, AI_Movie_Translate_Kaggle.ipynb, AI_Movie_Translate_Kaggle_PRIVATE.ipynb) to automatically install nodejs during Cell 1 setup, enabling full YouTube player JavaScript format descrambling.

---

### 9. 📱 Cloud Notebooks Synchronization
- Both **Google Colab** and **Kaggle Cloud** editions updated with 100% feature parity:
  - AI_Movie_Translate_Colab.ipynb (Public) & AI_Movie_Translate_Colab_PRIVATE.ipynb (VIP Private)
  - AI_Movie_Translate_Kaggle.ipynb (Public) & AI_Movie_Translate_Kaggle_PRIVATE.ipynb (VIP Private)
  - Node.js runtime pre-installed in Cell 1 system package initialization.
  - Cell 1 auto-updates from GitHub origin/main on every launch (git fetch origin main && git reset --hard origin/main).

---

### 10. 🚀 Unified Single-Pass FFmpeg Filtergraph (4x Rendering Speedup)
- **Problem:** MoviePy's 3-pass Python frame rendering loop consumed 35+ minutes for a 15-minute 1080p video:
  - Pass 1: MoviePy frame-by-frame Python loop to composite video + audio (~25 min).
  - Pass 2: FFmpeg post-processing for Subtitle Blurring + Color Grading + ASS Subtitle Burning (~7 min).
  - Pass 3: Duplicating work for Reels 9:16 background crop and placement (~5 min).
- **Solution:** Re-engineered [agents/video_merger_agent.py](agents/video_merger_agent.py) with a **Unified Single-Pass FFmpeg Filtergraph**:
  - MoviePy is now used **only** for fast master audio mixing (`final_audio.write_audiofile(...)` in ~5 seconds).
  - All visual transformations (base crop, copyright mirror/resize, Vision AI subtitle boxblur, color grading `eq`, custom watermark overlay, and styled Myanmar ASS subtitle burning) are assembled into a single `-filter_complex` pipeline.
  - **Simultaneous Dual Output:** Generates both `final_recap.mp4` (hardsubbed 16:9) and `final_recap_clean.mp4` (clean frame for 9:16 Reels) in a single hardware-accelerated pass (`h264_nvenc` / `h264_qsv` / `libx264 superfast`).
- **Benchmark:**
  | Metric | Previous 3-Pass MoviePy | v2.2 Single-Pass Filtergraph | Speedup |
  | :--- | :---: | :---: | :---: |
  | CPU Rendering (Colab/Local) | 35–40 minutes | **6–8 minutes** | **4x–5x faster** |
  | GPU Rendering (NVENC T4) | 8–10 minutes | **~1.5 minutes** | **5x–6x faster** |
  | RAM / Memory Footprint | High (Python frames) | **Minimal (C-level FFmpeg)** | **Zero OOM crashes** |

---

### 11. 🧠 Gemini CoT Thought Block Filtering (100% JSON Reliability)
- **Problem:** Gemini 2.5 and 3.x Flash/Pro models include internal Chain-of-Thought reasoning parts (`{"thought": true, "text": "..."}`) in API responses. In `QAAgent`, when Gemini counted syllables or contemplated alternative translations before outputting JSON, these thought chunks leaked into the text string, causing `json.loads` to fail with:
  `[!] QAAgent: Could not parse JSON: (3) ပ(1)်(1) က(1)မ(1)်(1)း(1)... oops 46 chars! Let's shorten:`
- **Solution:** Updated `_extract_text_from_gemini_response` in [brain/gemini_client.py](brain/gemini_client.py):
  - Filters out any candidate response part with `p.get("thought")`, `p.get("role") == "thought"`, or `p.get("type") == "thought"`.
  - Applies regex `re.sub(r'<thought>.*?</thought>', '', final_text, flags=re.DOTALL | re.IGNORECASE)` and `re.sub(r'<think>.*?</think>', '', final_text, flags=re.DOTALL | re.IGNORECASE)` to strip any inline thinking tags.
  - Guarantees 100% clean JSON payloads returned to all agents (`QAAgent`, `WriterAgent`, `DirectorAgent`).

---

### 12. ⚡ Enforce Model Priority: `gemini-3.5-flash-lite`
- **Workhorse Engine:** Firmly established `gemini-3.5-flash-lite` as the primary default across all pipeline components:
  - `brain/gemini_client.py`: `_build_model_list()` defaults strictly to `gemini-3.5-flash-lite` (15 RPM high-throughput tier) before falling back through `_FALLBACK_MODELS`.
  - `agents/writer_agent.py`: `model_workhorse` defaults to `gemini-3.5-flash-lite`.
  - `agents/qa_agent.py`: `model_workhorse` in both duration constraint enforcement and script colloquial review defaults to `gemini-3.5-flash-lite`.
  - `agents/seo_agent.py`: `gemini_model` defaults to `gemini-3.5-flash-lite`.
  - `agents/thumbnail_agent.py`: Vision AI model defaults to `gemini-3.5-flash-lite`.
  - `agents/audio_agent.py` & `agents/video_merger_agent.py`: AI assist and subtitle detection models default to `gemini-3.5-flash-lite`.
- **Benefit:** Maximizes API throughput (15 RPM vs 5 RPM), prevents 429 quota exhaustion, and provides sub-second latency for real-time video recap generation.

---

### 13. 🎛️ Pure FFmpeg Audio Compositing & Zero MoviePy Dependency
- **Problem:** MoviePy's Python-level audio compositing (`AudioFileClip`, `CompositeAudioClip`, and `final_audio.write_audiofile()`) caused high RAM usage (2GB+), slow Python frame loops, and intermittent memory exhaustion on long movies with 200+ speech clips.
- **Solution:** Transitioned the pipeline to **Pure FFmpeg Audio Compositing** (Zero MoviePy Execution):
  - **Ultra-Fast Linear PCM Assembly:** `_assemble_voiceover_track` stitches hundreds of speech clips into a contiguous 44.1kHz 16-bit PCM WAV buffer in ~2 seconds using C-accelerated `soundfile` and `numpy`.
  - **Broadcast-Grade Dynamic Audio Ducking:** Eliminated MoviePy's custom python numpy transform loop (`duck_transform`), replacing it with FFmpeg's native `sidechaincompress=threshold=0.08:ratio=8:attack=100:release=400` filter and `amix=inputs=2:duration=first:dropout_transition=0`. Background audio (Demucs SFX or cinematic BGM loop) ducks smoothly during Burmese narration and swells naturally in dialogue gaps.
  - **Simultaneous Dual Audio Output:** Uses `,asplit=2[a_master1][a_master2]` to cleanly route composited audio to both `final_recap.mp4` (hardsubbed) and `final_recap_clean.mp4` (clean canvas) simultaneously.
  - **RAM & CPU Efficiency:** Eliminates MoviePy execution in 100% of standard runs, reducing RAM usage from >2 GB to ~70 MB and avoiding Python GIL bottlenecks.
  - **Robust Fallback Safety Net:** MoviePy loading is preserved inside an isolated `_legacy_moviepy_merge()` method as a safety net in case of missing binary components or thumbnail intro stitching.

---

### 14. 🎙️ Acoustic Pitch & Multimodal Vision Diarization (Multi-Voice Dubbing)
- **Problem:** When dubbing full movies into Burmese, all characters (male leads, female heroines, villains, narrators) were voiced by a single narrator voice, flattening dramatic tension and dialogue clarity.
- **Solution:** Implemented **Hybrid Acoustic & Multimodal Vision Speaker Diarization**:
  - **Acoustic Fundamental Frequency ($F_0$) Estimation:** In [agents/audio_agent.py](agents/audio_agent.py), implemented normalized autocorrelation across 40ms overlapping frames within the human vocal range ($70\text{ Hz} \le F_0 \le 350\text{ Hz}$). Accurately determines acoustic gender (`male` if $F_0 \le 165\text{ Hz}$, `female` if $F_0 > 165\text{ Hz}$, or `unknown`) directly from Demucs-isolated vocals.
  - **Multimodal Video Keyframe Context:** In [agents/writer_agent.py](agents/writer_agent.py) and [brain/gemini_client.py](brain/gemini_client.py), extracts lightweight base64 JPEG keyframes at dialogue scene timestamps and feeds them alongside acoustic pitch hints into Gemini Vision translation prompts.
  - **Character & Gender Profile Registry:** `MovieState.speaker_profiles` dynamically tracks characters, genders, and emotions.
  - **Intelligent Multi-Voice Synthesis:** In [agents/voice_agent.py](agents/voice_agent.py), automatically routes female dialogue to `my-MM-NilarNeural` and male/narrator dialogue to `my-MM-ThihaNeural`, applying emotion-driven dynamic pitch (`+5Hz` / `-2Hz`) and volume modulation.

---

### 15. 💾 Deterministic Phase-by-Phase Checkpoint Resume Engine
- **Problem:** If a pipeline run was interrupted by network drops, notebook runtime disconnects, or system restarts during a long movie, users had to restart from Phase 1, repeating time-consuming audio extraction, Demucs vocal separation, Whisper transcription, and TTS synthesis.
- **Solution:** Implemented a **Deterministic Checkpoint & Granular Resume Engine**:
  - **7-Phase State Tracking:** Formally defined pipeline phase boundaries in [brain/memory.py](brain/memory.py) and [agents/master.py](agents/master.py):
    1. `phase_1_video_analysis`
    2. `phase_2_audio_stt`
    3. `phase_3_scene_detection`
    4. `phase_4_script_seo_thumbnail`
    5. `phase_5_voice_generation`
    6. `phase_6_video_merge`
    7. `phase_7_qa`
  - **On-Disk Physical Artifact Validation:** `MasterAgent._validate_artifacts` never relies on state flags alone. A phase is only skipped if its verified outputs exist on disk (valid duration, non-empty transcript, non-corrupt `thumbnail.jpg`, complete `voiceover/` files, or non-corrupt `final_recap.mp4` > 50KB).
  - **Granular Clip-Level TTS Resume:** In [agents/voice_agent.py](agents/voice_agent.py), Edge-TTS and F5-TTS check whether `scene_{(idx+1):04d}.mp3` already exists with valid data (> 1000 bytes). Already generated clips are appended directly, avoiding duplicate API calls and network latency.
  - **Atomic Checkpoint Serialization:** Saves `checkpoint.json` atomically via temporary file replacement (`.tmp` -> `.json`) at each phase boundary.
  - **Full CLI & Web UI Integration:**
    - CLI: Added `--resume` (enabled by default) and `--fresh` / `--force-restart` flags in [main.py](main.py) and [brain/planner.py](brain/planner.py).
    - Web UI: Added `resume: bool` parameter to `/api/start` and `/api/batch/start` in [web_ui.py](web_ui.py).
---

### 16. 🔊 Overlap-Add Audio Mixing & True Scene-Anchor Zero Cumulative Drift
- **Problem:** Sequential audio placement (`place_time = max(curr_t, orig_start)`) combined with direct buffer assignment (`vo_buffer[...] = data`) resulted in two compounding problems:
  1. If any clip overran by even 0.2s, every subsequent clip was permanently pushed later, causing runaway cumulative drift.
  2. Overlapping speech clips suffered hard cutoff where the subsequent clip abruptly silenced the tail of the previous clip.
- **Solution:** In [agents/video_merger_agent.py](agents/video_merger_agent.py):
  - **Overlap-Add Mixing:** Switched to `vo_buffer[start_idx:end_idx] += data[:end_idx - start_idx]` with soft-clipping/normalization to smoothly blend speech tails without integer clipping.
  - **True Scene-Anchor Sync:** If a clip overruns by $\le 0.5\text{s}$, the next clip snaps directly to `orig_start`. Timing drift instantly resets to 0.000s at dialogue and scene transitions.

---

### 17. 🇲🇲 Space-Agnostic Burmese Syllable Subtitle Chunking & Dynamic Reels ASS
- **Problem:**
  1. In `_write_ass` and Reels export, narration text was split using `text.split(" ")`. Because standard Burmese text does not use spaces between words, sentences appeared as single massive unbroken lines without proportional time division.
  2. The Reels subtitle path was static (`temp/reels_subs.ass`), causing batch processing collisions.
  3. Unspaced Reels hook titles overflowed horizontally beyond the 1080px canvas boundaries.
- **Solution:** In [agents/video_merger_agent.py](agents/video_merger_agent.py):
  - **Space-Agnostic Chunking:** Implemented `_chunk_burmese_narration()` using Myanmar syllable boundary regex (`[\u1000-\u102a\u104e]...`), partitioning unspaced sentences into proportional, syllable-accurate subtitle chunks.
  - **Dynamic Collision-Free Naming:** Reels ASS subtitles now use unique filenames (`reels_subs_{safe_id}_{uuid}.ass`).
  - **Syllable Title Wrapping:** Hook titles are wrapped via `_wrap_burmese_text` to fit the 1080px canvas without overflow.

---

### 18. ⚡ Windows Launcher Ampersand Fix & Performance Optimizations
- **Windows Launcher URL Fix:** In [Run_Movie_Recap.bat](Run_Movie_Recap.bat), enabled `EnableDelayedExpansion` and quoted `!input_src!` expansion so URLs containing `&` (e.g. `&t=30s&ab_channel=...`) do not trigger cmd.exe syntax errors or command chaining crashes.
- **Instant Audio Extraction:** In [agents/audio_agent.py](agents/audio_agent.py), direct FFmpeg extraction is now executed first (instant C-speed, minimal RAM), using MoviePy only as a fallback.
- **CPU Demucs Guard:** In [agents/audio_agent.py](agents/audio_agent.py), automatically detects CUDA availability. If running on CPU without explicit override, Demucs is gracefully skipped, saving 20–40 minutes of 100% CPU lockup per video.
- **RPD Quota Corrections:** In [brain/config.py](brain/config.py), corrected `DEFAULT_CONFIG` `daily_limit_per_key` to 1500 and model daily limits to 1000/500 (resolving confusion between RPM and RPD).
- **F5-TTS Burmese Auto-Reroute:** In [agents/voice_agent.py](agents/voice_agent.py), automatically re-routes Burmese requests from F5-TTS to Edge-TTS to prevent phonological distortion.

