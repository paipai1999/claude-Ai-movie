"""
Tests for the shared option model (core/options.py), the request schemas generated from it
(services/schemas.py) and the worker-argument builder (services/job_args.py).

The most important test replays the PREVIOUS hand-written handler logic and proves the new,
generated path produces byte-identical worker arguments (queued jobs are stored positionally).
"""

import itertools
import json
import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core import options as O
from services import job_runtime as jr
from services.job_args import build_worker_args, worker_option_names
from services.schemas import BatchStartRequest, StartRequest

# ── The contract of the API before the option model existed (frozen on purpose) ─────────────
LEGACY_START_FIELDS = {
    "input", "engine_mode", "project_name", "force_whisper", "language", "subtitle_mode",
    "resolution", "tts_engine", "tts_voice", "custom_thumb_title", "watermark_enabled",
    "watermark_text", "watermark_opacity", "reels_enabled", "video_format", "subtitle_style",
    "thumbnail_intro", "source_language", "skip_demucs", "detect_scenes", "script_engine",
    "resume", "trim_end", "no_smart_trim", "outro_card", "blur_mode", "blur_height", "mirror",
    "color_grading", "audio_anti_copyright", "translation_style", "audio_mode", "sfx_mode",
    "sfx_volume", "render_video", "stage_toggles", "context_hint",
}
LEGACY_BATCH_FIELDS = (LEGACY_START_FIELDS - {"input", "project_name", "trim_end", "no_smart_trim", "outro_card"}) | {"inputs"}

# Values the OLD request models filled in when a client omitted a field.
LEGACY_DEFAULTS = {
    "engine_mode": "recap", "project_name": None, "force_whisper": False, "language": "burmese",
    "subtitle_mode": "burn", "resolution": "1080p", "tts_engine": None, "tts_voice": None,
    "custom_thumb_title": None, "watermark_enabled": True, "watermark_text": None,
    "watermark_opacity": None, "reels_enabled": True, "video_format": "both",
    "subtitle_style": "box_black", "thumbnail_intro": False, "source_language": "auto",
    "skip_demucs": False, "detect_scenes": False, "script_engine": "recap", "resume": True,
    "trim_end": None, "no_smart_trim": False, "outro_card": False, "blur_mode": "auto",
    "blur_height": None, "mirror": False, "color_grading": True, "audio_anti_copyright": False,
    "translation_style": "recap", "audio_mode": "ai_voiceover", "sfx_mode": "original_sfx",
    "sfx_volume": 0.15, "render_video": True, "stage_toggles": None, "context_hint": None,
}


# ── The OLD handler logic, copied verbatim from web_ui.py (before the refactor) ──────────────
def legacy_single_args(d, job_id="J"):
    """Returns (worker_name, args) exactly as the previous start_pipeline built them."""
    input_source = d["input"]
    language = d["language"] or "burmese"
    subtitle_mode = d["subtitle_mode"] or "burn"
    subtitle_style = d["subtitle_style"] or "box_black"
    resolution = d["resolution"] or "1080p"
    video_format = d["video_format"] or ("both" if d["reels_enabled"] else "16:9")

    if d["engine_mode"] == "hardsub":
        return "hardsub_worker", (
            job_id, input_source, d["project_name"], d["source_language"] or "auto",
            d["force_whisper"] or False, video_format, resolution, subtitle_style,
            d["blur_mode"] or "auto", d["mirror"] or False,
            d["color_grading"] if d["color_grading"] is not None else True,
            d["blur_height"], d["audio_anti_copyright"] or False,
            d["translation_style"] or "persona", d["audio_mode"] or "original",
            d["sfx_mode"] or "original_sfx",
            d["sfx_volume"] if d["sfx_volume"] is not None else 0.15,
            d["render_video"] if d["render_video"] is not None else True,
            d["stage_toggles"], d["context_hint"],
        )
    if d["engine_mode"] == "subtitle":
        return "subtitle_worker", (
            job_id, input_source, d["project_name"], d["source_language"] or "auto",
            d["force_whisper"] or False, d["translation_style"] or "dialogue",
            d["audio_mode"] or "original", d["sfx_mode"] or "original_sfx",
            d["sfx_volume"] if d["sfx_volume"] is not None else 0.15,
            d["render_video"] if d["render_video"] is not None else False,
            video_format, resolution, subtitle_style, d["blur_mode"] or "auto",
            d["mirror"] or False,
            d["color_grading"] if d["color_grading"] is not None else True,
            d["blur_height"], d["audio_anti_copyright"] or False,
            d["stage_toggles"], d["context_hint"],
        )
    if d["engine_mode"] == "download":
        return "downloader_worker", (job_id, input_source, resolution or "1080p", d["project_name"])
    return "pipeline_worker", (
        job_id, input_source, language, subtitle_mode, resolution, d["tts_engine"],
        d["custom_thumb_title"], d["watermark_enabled"], d["watermark_text"],
        d["watermark_opacity"], d["reels_enabled"], video_format, subtitle_style,
        d["thumbnail_intro"], d["source_language"] or "auto", d["skip_demucs"] or False,
        d["detect_scenes"] or False, d["resume"] if d["resume"] is not None else True,
        d["tts_voice"], d["script_engine"] or "recap", d["trim_end"],
        d["no_smart_trim"] or False, d["outro_card"] or False,
        d["translation_style"] or "recap", d["audio_mode"] or "ai_voiceover",
        d["sfx_mode"] or "original_sfx",
        d["sfx_volume"] if d["sfx_volume"] is not None else 0.15,
        d["blur_mode"] or "auto", d["blur_height"], d["mirror"] or False,
        d["audio_anti_copyright"] or False,
        d["render_video"] if d["render_video"] is not None else True,
        d["stage_toggles"], d["context_hint"],
    )


def legacy_batch_args(d, inputs, job_id="J"):
    """Previous start_batch_pipeline arguments (translation_style / audio_mode were passed raw and
    the batch worker applied the per-engine fallback; that fallback is applied here to compare the
    EFFECTIVE value)."""
    language = d["language"] or "burmese"
    video_format = d["video_format"] or ("both" if d["reels_enabled"] else "16:9")
    engine = d["engine_mode"] or "recap"
    ts_default = {"hardsub": "persona", "subtitle": "dialogue"}.get(engine, "recap")
    am_default = {"hardsub": "original", "subtitle": "original"}.get(engine, "ai_voiceover")
    return (
        job_id, inputs, language, d["subtitle_mode"] or "burn", d["resolution"] or "1080p",
        d["tts_engine"], d["custom_thumb_title"], d["watermark_enabled"], d["watermark_text"],
        d["watermark_opacity"], d["reels_enabled"], video_format,
        d["subtitle_style"] or "box_black", d["thumbnail_intro"],
        d["source_language"] or "auto", d["skip_demucs"] or False, d["detect_scenes"] or False,
        d["resume"] if d["resume"] is not None else True, d["tts_voice"],
        d["script_engine"] or "recap", engine, d["blur_mode"] or "auto", d["blur_height"],
        d["mirror"] or False,
        d["color_grading"] if d["color_grading"] is not None else True,
        d["audio_anti_copyright"] or False, d["force_whisper"] or False,
        d["translation_style"] or ts_default, d["audio_mode"] or am_default,
        d["sfx_mode"] or "original_sfx",
        d["sfx_volume"] if d["sfx_volume"] is not None else 0.15,
        # The old batch handler forced ``else True`` here, which defeated the batch worker's own
        # "subtitle engine renders no video unless asked" fallback (the single-job handler was
        # already correct). The engine-correct value is the intended behaviour.
        d["render_video"] if d["render_video"] is not None else (engine != "subtitle"),
        d["stage_toggles"], d["context_hint"],
    )


WORKERS = {
    "hardsub_worker": jr.hardsub_worker, "subtitle_worker": jr.subtitle_worker,
    "downloader_worker": jr.downloader_worker, "pipeline_worker": jr.pipeline_worker,
}


def new_single_args(d, job_id="J"):
    engine = O.normalize_engine(d["engine_mode"])
    opts = O.resolve(engine, d)
    worker = {"hardsub": jr.hardsub_worker, "subtitle": jr.subtitle_worker,
              "download": jr.downloader_worker}.get(engine, jr.pipeline_worker)
    return worker.__name__, build_worker_args(worker, job_id, opts)


def new_batch_args(d, inputs, job_id="J"):
    opts = O.resolve(O.normalize_engine(d["engine_mode"]), d)
    return build_worker_args(jr.batch_worker, job_id, opts, inputs=inputs)


def _variants():
    """A grid of requests: defaults, each field set to None / '' / a non-default value, and
    combinations of engine x video_format x reels toggle."""
    base = dict(LEGACY_DEFAULTS, input="movies/a.mp4")
    yield dict(base)
    nondefault = {
        "project_name": "My Proj", "force_whisper": True, "language": "english",
        "subtitle_mode": "none", "resolution": "720p", "tts_engine": "f5", "tts_voice": "v1",
        "custom_thumb_title": "T", "watermark_enabled": False, "watermark_text": "wm",
        "watermark_opacity": 0.7, "reels_enabled": False, "video_format": "9:16",
        "subtitle_style": "yellow_pop", "thumbnail_intro": True, "source_language": "zh",
        "skip_demucs": True, "detect_scenes": True, "script_engine": "drama_novel",
        "resume": False, "trim_end": 12.5, "no_smart_trim": True, "outro_card": True,
        "blur_mode": "yes", "blur_height": 0.2, "mirror": True, "color_grading": False,
        "audio_anti_copyright": True, "translation_style": "wuxia", "audio_mode": "original",
        "sfx_mode": "mute", "sfx_volume": 0.4, "render_video": False,
        "stage_toggles": {"qa": False}, "context_hint": "hint",
    }
    for key, value in nondefault.items():
        yield dict(base, **{key: value})
    for key in LEGACY_DEFAULTS:
        if key == "engine_mode":
            continue
        yield dict(base, **{key: None})
        if isinstance(LEGACY_DEFAULTS[key], str):
            yield dict(base, **{key: ""})
    yield dict(base, video_format="", reels_enabled=False)
    yield dict(base, video_format=None, reels_enabled=True)
    yield dict(base, video_format=None, reels_enabled=None)
    yield dict(base, **nondefault)


ENGINE_MODES = ["recap", "subtitle", "hardsub", "download", "something-unknown", None, ""]


class TestLegacyEquivalence(unittest.TestCase):
    def test_single_job_arguments_are_identical_to_the_old_handler(self):
        checked = 0
        for engine, variant in itertools.product(ENGINE_MODES, _variants()):
            d = dict(variant, engine_mode=engine)
            with self.subTest(engine=engine, variant={k: v for k, v in d.items() if LEGACY_DEFAULTS.get(k) != v}):
                self.assertEqual(new_single_args(d), legacy_single_args(d))
            checked += 1
        self.assertGreater(checked, 400)

    def test_batch_job_arguments_are_identical_to_the_old_handler(self):
        inputs = ["a.mp4", "https://x/y"]
        for engine, variant in itertools.product(["recap", "subtitle", "hardsub"], _variants()):
            d = dict(variant, engine_mode=engine)
            with self.subTest(engine=engine):
                self.assertEqual(new_batch_args(d, inputs), legacy_batch_args(d, inputs))

    def test_workers_receive_the_argument_count_they_declare(self):
        for name, worker in list(WORKERS.items()) + [("batch_worker", jr.batch_worker)]:
            import inspect
            expected = len(inspect.signature(worker).parameters)
            d = dict(LEGACY_DEFAULTS, input="x", engine_mode="recap")
            opts = O.resolve("recap", d)
            args = build_worker_args(worker, "J", opts, inputs=["x"])
            self.assertEqual(len(args), expected, name)


class TestApiContract(unittest.TestCase):
    def test_start_request_has_exactly_the_previous_fields(self):
        self.assertEqual(set(StartRequest.model_fields), LEGACY_START_FIELDS)

    def test_batch_request_has_exactly_the_previous_fields(self):
        self.assertEqual(set(BatchStartRequest.model_fields), LEGACY_BATCH_FIELDS)

    def test_input_is_required_and_inputs_is_required_for_batch(self):
        with self.assertRaises(Exception):
            StartRequest()
        with self.assertRaises(Exception):
            BatchStartRequest()
        self.assertEqual(StartRequest(input="a").input, "a")
        self.assertEqual(BatchStartRequest(inputs=["a"]).inputs, ["a"])

    def test_unchanged_defaults_for_non_engine_specific_options(self):
        req = StartRequest(input="a")
        for name, value in LEGACY_DEFAULTS.items():
            if name in ("translation_style", "audio_mode", "render_video"):
                continue  # engine-specific, see TestEngineDefaults
            self.assertEqual(getattr(req, name), value, name)


class TestEngineDefaults(unittest.TestCase):
    """What happens when the client did NOT choose: the engine's own default (previously the
    recap value leaked into every engine because the API default was always truthy)."""

    def _resolved(self, engine):
        req = StartRequest(input="a", engine_mode=engine)
        return O.resolve(O.normalize_engine(req.engine_mode), req.model_dump())

    def test_translation_style_follows_the_engine(self):
        self.assertEqual(self._resolved("recap")["translation_style"], "recap")
        self.assertEqual(self._resolved("subtitle")["translation_style"], "dialogue")
        self.assertEqual(self._resolved("hardsub")["translation_style"], "persona")

    def test_audio_mode_follows_the_engine(self):
        self.assertEqual(self._resolved("recap")["audio_mode"], "ai_voiceover")
        self.assertEqual(self._resolved("subtitle")["audio_mode"], "original")
        self.assertEqual(self._resolved("hardsub")["audio_mode"], "original")

    def test_subtitle_engine_does_not_render_video_unless_asked(self):
        self.assertFalse(self._resolved("subtitle")["render_video"])
        self.assertTrue(self._resolved("hardsub")["render_video"])
        self.assertTrue(self._resolved("recap")["render_video"])

    def test_explicit_choice_always_wins(self):
        req = StartRequest(input="a", engine_mode="hardsub", translation_style="wuxia",
                           audio_mode="ai_voiceover", render_video=False)
        r = O.resolve("hardsub", req.model_dump())
        self.assertEqual((r["translation_style"], r["audio_mode"], r["render_video"]),
                         ("wuxia", "ai_voiceover", False))


class TestOptionModelIntegrity(unittest.TestCase):
    def test_every_option_belongs_to_a_known_step_and_engine(self):
        for opt in O.OPTIONS:
            self.assertIn(opt.step, O.STEP_IDS, opt.name)
            self.assertTrue(opt.engines <= O.ALL, opt.name)
            self.assertTrue(set(opt.engine_defaults) <= opt.engines, f"{opt.name}: default for non-applicable engine")

    def test_steps_are_asked_in_a_fixed_order(self):
        self.assertEqual(O.STEP_IDS, ("source", "language", "output", "protection", "audio"))
        flow_ids = [s.id for s, _ in O.steps_for("hardsub")]
        self.assertEqual(flow_ids, sorted(flow_ids, key=O.STEP_IDS.index))

    def test_download_engine_only_asks_what_it_needs(self):
        asked = {o.name for _, opts in O.steps_for("download") for o in opts}
        self.assertEqual(asked, {"engine_mode", "input", "project_name", "resolution"})

    def test_engine_specific_options_are_hidden_from_other_engines(self):
        hardsub = {o.name for o in O.options_for("hardsub")}
        self.assertNotIn("tts_voice", hardsub)
        self.assertNotIn("watermark_text", hardsub)
        self.assertIn("blur_mode", hardsub)

    def test_describe_is_json_serialisable_and_complete(self):
        text = json.dumps(O.describe())
        data = json.loads(text)
        self.assertEqual(set(data["options"]), {o.name for o in O.OPTIONS})
        self.assertEqual([s["id"] for s in data["steps"]], list(O.STEP_IDS))

    def test_unknown_choices_are_reported_not_rejected(self):
        r = O.resolve("hardsub", dict(LEGACY_DEFAULTS, input="x", engine_mode="hardsub", blur_mode="maybe"))
        self.assertEqual(r["blur_mode"], "maybe")
        self.assertTrue(any("blur_mode" in w for w in O.unknown_choices("hardsub", r)))


class TestWorkerArgBuilder(unittest.TestCase):
    def test_missing_value_fails_loudly(self):
        def fake_worker(job_id, input_source, not_an_option):
            pass
        with self.assertRaises(KeyError):
            build_worker_args(fake_worker, "J", {"input": "x"})

    def test_aliases(self):
        self.assertEqual(worker_option_names(jr.downloader_worker), ["input", "resolution", "project_name"])
        self.assertEqual(worker_option_names(jr.batch_worker)[0], "inputs")


if __name__ == "__main__":
    unittest.main()
