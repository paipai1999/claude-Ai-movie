"""
Static checks for static/js/tools.js (the tool hub, the Long to Short / Splitter panels and the result viewer)
against the option model, studio.js, the template and the stylesheet. A mistyped key or option name only shows up
in the browser as a blank label or a control that posts nothing, so the files are compared directly.
(tests/ui/dashboard_smoke.js covers the live behaviour.)
"""

import json
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import options as O  # noqa: E402


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


HTML = read("templates", "index.html")
STUDIO = read("static", "js", "studio.js")
TOOLS = read("static", "js", "tools.js")
CSS = read("static", "css", "studio.css")


def text_blocks():
    """{language: {key: text}} read from the TEXT object of tools.js."""
    start = TOOLS.index("const TEXT = {")
    end = TOOLS.index("function tt(key, vars)")
    body = TOOLS[start:end]
    parts = re.split(r"(?m)^        (en|my|th): \{", body)
    out = {}
    for i in range(1, len(parts), 2):
        out[parts[i]] = dict(re.findall(r"(?:^|[\s,{])([A-Za-z_][A-Za-z0-9_]*):\s*'((?:[^'\\]|\\.)*)'", parts[i + 1]))
    return out


def studio_keys():
    start = STUDIO.index("const I18N = {")
    end = STUDIO.index("function t(key, vars)")
    parts = re.split(r"(?m)^        (en|my|th): \{", STUDIO[start:end])
    return {parts[i]: set(re.findall(r"(?m)(?:^|[\s,{])([A-Za-z_][A-Za-z0-9_]*):\s*['\"]", parts[i + 1])) for i in range(1, len(parts), 2)}


def tool_plans():
    """{tool: (engine, basic, advanced)} read from the TOOLS table of tools.js."""
    block = TOOLS[TOOLS.index("const TOOLS = {"):TOOLS.index("// [min, max, step]")]
    plans = {}
    for match in re.finditer(r"(\w+): \{\s*engine: '(\w+)',.*?basic: \[([^\]]*)\],\s*advanced: \[([^\]]*)\]", block, re.S):
        tool, engine, basic, advanced = match.groups()
        plans[tool] = (engine, re.findall(r"'(\w+)'", basic), re.findall(r"'(\w+)'", advanced))
    return plans


class TextTests(unittest.TestCase):
    def test_all_three_languages_have_the_same_keys(self):
        blocks = text_blocks()
        self.assertEqual(set(blocks), {"en", "my", "th"})
        for lang in ("my", "th"):
            self.assertEqual(set(blocks["en"]) - set(blocks[lang]), set(), f"{lang} is missing keys")
            self.assertEqual(set(blocks[lang]) - set(blocks["en"]), set(), f"{lang} has keys English lacks")
        self.assertGreater(len(blocks["en"]), 100)

    def test_burmese_and_thai_are_really_translated(self):
        blocks = text_blocks()
        for lang, pattern in (("my", r"[က-႟]"), ("th", r"[฀-๿]")):
            translated = sum(1 for text in blocks[lang].values() if re.search(pattern, text))
            self.assertGreater(translated, len(blocks[lang]) * 0.7, f"{lang} looks untranslated")

    def test_every_literal_key_the_script_asks_for_exists(self):
        known = set(text_blocks()["en"])
        used = set(re.findall(r"\btt\('([A-Za-z0-9_]+)'\s*[,)]", TOOLS))       # literal keys; 'opt_' + name is built at run time
        self.assertGreater(len(used), 30)
        self.assertEqual(used - known, set())
        for tile_key in re.findall(r"(?:title|desc): '(tile\w+)'", TOOLS):
            self.assertIn(tile_key, known)

    def test_studio_texts_used_by_the_tool_panels_exist(self):
        keys = studio_keys()
        used = set(re.findall(r"studio\.t\('([A-Za-z0-9_]+)'\)", TOOLS))
        used |= {"optDeliveryOneclick", "optDeliveryOneclickDesc", "optDeliveryCapcut", "optDeliveryBoth", "optDelivery"}
        for lang, known in keys.items():
            self.assertEqual(used - known, set(), lang)


class OptionTests(unittest.TestCase):
    def test_each_panel_only_shows_options_of_its_engine(self):
        plans = tool_plans()
        self.assertEqual(set(plans), {"shorts", "splitter"})
        for tool, (engine, basic, advanced) in plans.items():
            self.assertEqual(engine, tool)
            self.assertIn(engine, O.ENGINES)
            for name in basic + advanced:
                self.assertIn(name, O.BY_NAME, f"{tool}: unknown option {name}")
                self.assertTrue(O.BY_NAME[name].applies_to(engine), f"{tool}: {name} is not an option of {engine}")

    def test_every_option_of_a_tool_is_reachable_in_its_panel(self):
        """No tool option may exist only in the API: the panel shows it, or it is deliberately left to the default."""
        plans = tool_plans()
        left_to_default = {"engine_mode", "input", "translation_style"}
        for tool, (engine, basic, advanced) in plans.items():
            shown = set(basic + advanced)
            for option in O.options_for(engine):
                if option.name not in shown:
                    self.assertIn(option.name, left_to_default, f"{tool}: option {option.name} has no control")

    def test_labels_exist_for_every_control_and_every_choice(self):
        blocks = text_blocks()
        for tool, (engine, basic, advanced) in tool_plans().items():
            for lang, texts in blocks.items():
                self.assertIn("title_" + tool, texts)
                self.assertIn("desc_" + tool, texts)
                for name in basic + advanced:
                    if name == "delivery_mode":
                        continue                          # its texts come from studio.js
                    self.assertIn("opt_" + name, texts, f"{lang}: no label for {name}")
                    for choice in O.BY_NAME[name].choices:
                        key = "ch_%s_%s" % (name, {"9:16": "916", "16:9": "169", "1:1": "11"}.get(choice, choice))
                        self.assertIn(key, texts, f"{lang}: no label for {name}={choice}")

    def test_number_controls_have_ranges_the_engines_accept(self):
        ranges = {name: tuple(float(n) for n in nums.split(",")) for name, nums in
                  re.findall(r"(\w+): \[([\d., ]+)\]", TOOLS[TOOLS.index("const RANGES"):TOOLS.index("const CHOICE_KEY")])}
        for name in ("clip_count", "clip_min_sec", "clip_max_sec", "split_minutes", "split_parts"):
            low, high, step = ranges[name]
            option = O.BY_NAME[name]
            self.assertLess(low, high)
            for engine in option.engines:
                self.assertGreaterEqual(option.default_for(engine), low, name)
                self.assertLessEqual(option.default_for(engine), high, name)
        # the engines' own limits: a Short is at least 8 s, a part at least 20 s, at most 20 Shorts
        self.assertGreaterEqual(ranges["clip_min_sec"][0], 8)
        self.assertLessEqual(ranges["clip_count"][1], 20)
        self.assertGreaterEqual(ranges["split_minutes"][0] * 60, 20)


class WiringTests(unittest.TestCase):
    def test_what_tools_js_takes_from_studio_js_is_exported(self):
        wanted = re.search(r"const \{([^}]+)\} = studio\.ui;", TOOLS).group(1)
        names = {n.strip() for n in wanted.split(",")}
        block = STUDIO[STUDIO.index("window.PaiStudio = {"):]
        ui_block = re.search(r"ui: \{([^}]+)\}", block).group(1)
        exported = {n.strip() for n in ui_block.split(",")}
        self.assertEqual(names - exported, set())

    def test_element_ids_exist(self):
        in_html = set(re.findall(r'\bid="([^"]+)"', HTML))
        created = set(re.findall(r"\bid: '([\w-]+)'", TOOLS))
        read_ids = set(re.findall(r"\$\('([\w-]+)'\)", TOOLS))
        self.assertEqual(read_ids - in_html - created, set())
        for needed in ("view-tool", "tool-grid", "tool-hub-title", "select-engine-mode", "header-title-text"):
            self.assertIn(needed, in_html)

    def test_the_hub_keeps_the_ids_the_template_translates(self):
        for tile_id in ("card-engine1-title", "card-engine1-desc", "card-engine2-title", "card-engine3-desc"):
            self.assertIn(f"'{tile_id}'", TOOLS)

    def test_the_hub_opens_what_exists(self):
        self.assertEqual(set(re.findall(r"startWizard\('(\w+)', '(\w+)'\)", TOOLS)),
                         {("recap", "oneclick"), ("recap", "capcut"), ("subtitle", "oneclick"), ("hardsub", "oneclick")})
        for engine in ("recap", "subtitle", "hardsub"):
            self.assertIn(f'<option value="{engine}"', HTML)
        self.assertIn('name="delivery_mode" value="capcut"', HTML)
        self.assertEqual(set(re.findall(r"openTool\('(\w+)'\)", TOOLS)) - {"shorts", "splitter"}, set())

    def test_global_functions_the_script_calls_exist_in_the_page(self):
        for name in ("switchView", "goToStep", "handleEngineChange", "handleDeliveryChange", "openVideoPlayerModal"):
            self.assertRegex(HTML, r"function %s\(" % name)

    def test_the_tool_view_calls_only_endpoints_that_exist(self):
        import web_ui
        def paths(routes):
            for route in routes:
                if getattr(route, "path", None):
                    yield route.path
                # FastAPI keeps the routes of an included router on ``original_router`` (older versions flatten them)
                for inner in getattr(getattr(route, "original_router", None), "routes", []) or []:
                    yield getattr(inner, "path", "")

        routes = set(paths(web_ui.app.routes))
        for path in ("/api/options", "/api/start", "/api/status/{job_id}", "/api/cancel/{job_id}", "/api/upload",
                     "/api/projects/{name}/tool-result"):
            self.assertTrue(any(r == path or r.endswith(path) for r in routes), path)
        for literal in ("/api/options", "/api/start", "/api/status/", "/api/cancel/", "/api/upload", "/tool-result"):
            self.assertIn(literal, TOOLS)

    def test_no_server_text_is_injected_as_html(self):
        self.assertNotIn("innerHTML", TOOLS)
        self.assertNotIn("insertAdjacentHTML", TOOLS)
        self.assertNotIn("document.write", TOOLS)


class CssTests(unittest.TestCase):
    def test_classes_created_by_the_script_are_styled(self):
        classes = set()
        for value in re.findall(r"class: '([^']+)'", TOOLS):
            classes.update(value.split())
        classes |= {"short-grid", "part-list"}                         # chosen with a ternary in renderResult
        classes |= {"is-done", "is-warn", "is-error", "is-running", "is-selected", "chip-ok", "chip-gold", "chip-warn"}
        classes = {c for c in classes if not c.endswith("-")}           # 'tool-tile tone-' + tone is completed at run time
        classes |= {f"tone-{tone}" for tone in re.findall(r"tone: '(\w+)'", TOOLS)}
        hooks = {"empty-state", "muted"}                 # shared helpers defined with the base styles
        missing = sorted(c for c in classes - hooks if not re.search(r"\." + re.escape(c) + r"(?![\w-])", CSS))
        self.assertEqual(missing, [])

    def test_the_delivery_picker_is_styled(self):
        for cls in ("delivery-picker", "delivery-cards", "delivery-card", "delivery-icon", "delivery-name", "delivery-desc",
                    "delivery-note", "is-overridden", "is-selected"):
            self.assertRegex(CSS, r"\." + cls + r"(?![\w-])")
        for cls in re.findall(r'class="((?:delivery)[^"]*)"', HTML):
            for one in cls.split():
                if one not in ("is-selected",):
                    self.assertRegex(CSS, r"\." + re.escape(one) + r"(?![\w-])")

    def test_every_tile_tone_has_colours(self):
        for tone in re.findall(r"tone: '(\w+)'", TOOLS):
            self.assertRegex(CSS, r"\.tone-" + tone + r"\s*\{")


class DescribeTests(unittest.TestCase):
    def test_the_option_model_exposes_what_the_panels_read(self):
        data = json.loads(json.dumps(O.describe()))
        for name in ("clip_count", "clip_focus", "short_subtitles", "short_framing", "split_mode", "split_snap", "delivery_mode"):
            self.assertIn(name, data["options"])
        self.assertEqual(data["options"]["video_format"]["engine_defaults"]["shorts"], "9:16")
        self.assertEqual(data["options"]["delivery_mode"]["choices"], ["oneclick", "capcut", "both"])


if __name__ == "__main__":
    unittest.main()
