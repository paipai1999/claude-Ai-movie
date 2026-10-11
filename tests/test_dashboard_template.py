"""
Static checks that the dashboard wizard (templates/index.html) follows the option model.

The browser behaviour (clicking through the steps, per-engine visibility, request payloads) is
covered by tests/ui/dashboard_smoke.js; these tests need no browser and run in CI.
"""

import os
import re
import sys
import unittest
from html.parser import HTMLParser

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core import options as O
from services.schemas import BatchStartRequest, StartRequest

TEMPLATE = os.path.join(PROJECT_ROOT, "templates", "index.html")

# The wizard has one card per step; the model's finer steps are merged into the cards below.
UI_STEP_OF_MODEL_STEP = {
    O.STEP_SOURCE: 1,
    O.STEP_LANGUAGE: 1,
    O.STEP_OUTPUT: 2,
    O.STEP_PROTECTION: 3,
    O.STEP_AUDIO: 4,
}


def _read():
    with open(TEMPLATE, encoding="utf-8") as handle:
        return handle.read()


class _Scan(HTMLParser):
    """Collects, for every element with data-option, the wizard step card that contains it."""

    def __init__(self):
        super().__init__()
        self.stack = []          # (tag, step or None)
        self.tagged = []         # (option, step, tag)
        self.ids_by_step = {}    # element id -> step

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        step = self.stack[-1][1] if self.stack else None
        match = re.fullmatch(r"wizard-step-(\d+)", attrs.get("id", ""))
        if match:
            step = int(match.group(1))
        if "data-option" in attrs:
            self.tagged.append((attrs["data-option"], step, tag))
        if attrs.get("id"):
            self.ids_by_step[attrs["id"]] = step
        if tag not in ("input", "br", "img", "meta", "link", "hr"):
            self.stack.append((tag, step))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break


class TestWizardFollowsTheModel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = _read()
        cls.scan = _Scan()
        cls.scan.feed(cls.html)

    def test_every_tagged_control_is_a_known_option(self):
        unknown = {name for name, _, _ in self.scan.tagged if name not in O.BY_NAME}
        self.assertFalse(unknown, f"data-option names missing from core/options.py: {unknown}")

    def test_every_control_sits_in_the_step_the_model_assigns(self):
        wrong = []
        for name, step, _tag in self.scan.tagged:
            expected = UI_STEP_OF_MODEL_STEP[O.BY_NAME[name].step]
            if step != expected:
                wrong.append(f"{name}: in wizard step {step}, model says step {expected}")
        self.assertFalse(wrong, "\n".join(wrong))

    def test_steps_run_in_the_models_order(self):
        steps = [UI_STEP_OF_MODEL_STEP[s] for s in O.STEP_IDS]
        self.assertEqual(steps, sorted(steps))

    def test_engine_is_chosen_before_the_options_it_changes(self):
        engine_step = self.scan.ids_by_step["select-engine-mode"]
        for control in ("select-translation-style", "select-audio-mode", "select-render-video",
                        "select-blur-mode", "select-video-format"):
            self.assertLessEqual(engine_step, self.scan.ids_by_step[control], control)

    def test_each_option_is_tagged_once(self):
        names = [name for name, _, _ in self.scan.tagged]
        self.assertEqual(len(names), len(set(names)), "an option is tagged on two elements")

    def test_markup_is_balanced(self):
        self.assertEqual(len(re.findall(r"<div\b", self.html)), len(re.findall(r"</div>", self.html)))
        self.assertEqual(self.html.count("</body>"), 1)
        self.assertEqual(self.html.count("</html>"), 1)


class TestPayloadFieldsAreApiFields(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        html = _read()
        block = html[html.index("const OPTION_READERS = {"):]
        block = block[:block.index("};")]
        cls.sent = re.findall(r"^\s{12}([a-z_]+):\s", block, re.M)

    def test_something_was_parsed(self):
        self.assertGreater(len(self.sent), 15)

    def test_every_sent_field_is_a_model_option(self):
        self.assertFalse([n for n in self.sent if n not in O.BY_NAME])

    def test_every_sent_field_is_accepted_by_both_endpoints(self):
        single = set(StartRequest.model_fields)
        batch = set(BatchStartRequest.model_fields)
        self.assertFalse([n for n in self.sent if n not in single], "not in StartRequest")
        self.assertFalse([n for n in self.sent if n not in batch], "not in BatchStartRequest")

    def test_payload_is_built_in_one_place(self):
        html = _read()
        self.assertEqual(html.count("fetch('/api/start'"), 0)
        self.assertEqual(html.count("fetch(url, {"), 1)
        self.assertEqual(html.count("collectJobOptions()"), 2)  # definition + the single call


class TestEngineDefaultsInTheFallbackMatchTheModel(unittest.TestCase):
    """The JS fallback table is only used when /api/options is unreachable; it must not drift."""

    def test_fallback_equals_model(self):
        html = _read()
        block = html[html.index("const FALLBACK_ENGINE_DEFAULTS = {"):]
        block = block[:block.index("};") + 2]
        for engine in ("recap", "subtitle", "hardsub"):
            row = re.search(engine + r":\s*\{([^}]*)\}", block).group(1)
            pairs = {k: v.strip("'\"") for k, v in re.findall(r"(\w+):\s*('[^']*'|\w+)", row)}
            for option in ("translation_style", "audio_mode", "render_video", "video_format"):
                expected = O.BY_NAME[option].default_for(engine)
                self.assertEqual(pairs[option], str(expected).lower() if isinstance(expected, bool) else expected,
                                 f"{engine}.{option}")


class TestLanguagesCoverTheSameStepStrings(unittest.TestCase):
    def test_step_strings_exist_in_every_language(self):
        html = _read()
        keys = [f"step{n}Nav" for n in range(1, 6)] + \
               [f"s{n}Title" for n in range(1, 6)] + [f"s{n}Desc" for n in range(1, 6)] + \
               ["btnStep1Next", "btnStep2Back", "btnStep2Next", "btnStep3Back", "btnStep3Next",
                "btnStep4Back", "btnStep4Next", "btnStep5Back"]
        for key in keys:
            self.assertEqual(len(re.findall(r"\n\s+" + key + r":", html)), 3, key)


if __name__ == "__main__":
    unittest.main()
