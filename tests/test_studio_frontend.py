"""
Static checks that templates/index.html, static/js/studio.js and static/css/studio.css agree with each other
(no browser needed; tests/ui/dashboard_smoke.js covers the live behaviour).

A mistyped translation key or element id only shows up as a blank label / dead button in the browser, so
these tests compare the three files directly.
"""

import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


HTML = read("templates", "index.html")
JS = read("static", "js", "studio.js")
CSS = read("static", "css", "studio.css")


def translation_keys():
    """{language: set(keys)} read from the I18N object of studio.js."""
    start = JS.index("const I18N = {")
    end = JS.index("function t(key, vars)")
    body = JS[start:end]
    blocks = re.split(r"(?m)^        (en|my|th): \{", body)
    out = {}
    for i in range(1, len(blocks), 2):
        out[blocks[i]] = set(re.findall(r"(?m)(?:^|[\s,{])([A-Za-z_][A-Za-z0-9_]*):\s*['\"]", blocks[i + 1]))
    return out


class TranslationTests(unittest.TestCase):
    def test_all_three_languages_have_the_same_keys(self):
        keys = translation_keys()
        self.assertEqual(set(keys), {"en", "my", "th"})
        for lang in ("my", "th"):
            self.assertEqual(keys["en"] - keys[lang], set(), f"{lang} is missing keys")
            self.assertEqual(keys[lang] - keys["en"], set(), f"{lang} has keys English lacks")
        self.assertGreater(len(keys["en"]), 100)

    def test_every_data_st_attribute_in_the_html_has_a_translation(self):
        used = set(re.findall(r'data-st(?:-placeholder)?="([A-Za-z0-9_]+)"', HTML))
        known = translation_keys()["en"]
        self.assertGreater(len(used), 20)
        self.assertEqual(used - known, set(), "data-st keys without a translation")

    def test_keys_used_by_the_script_exist(self):
        known = translation_keys()["en"]
        # literal keys only: t('issue_' + code) builds its key at run time
        used = set(re.findall(r"\bt\('([A-Za-z0-9_]+)'\s*[,)]", JS))
        self.assertEqual(used - known, set())

    def test_burmese_and_thai_are_really_translated(self):
        for lang, pattern in (("my", r"[က-႟]"), ("th", r"[฀-๿]")):
            block = JS[JS.index(f"        {lang}: {{"):]
            block = block[: block.index("\n        }")]
            self.assertGreater(len(re.findall(pattern, block)), 500, f"{lang} block looks untranslated")


class WiringTests(unittest.TestCase):
    def test_every_element_id_the_script_reads_exists_in_the_template(self):
        ids_in_html = set(re.findall(r'\bid="([^"]+)"', HTML))
        wanted = set(re.findall(r"\$\('([A-Za-z0-9_-]+)'\)", JS))
        self.assertEqual(wanted - ids_in_html, set(), "studio.js reads ids the page does not define")

    def test_template_calls_only_exported_studio_functions(self):
        exported_block = JS[JS.index("window.PaiStudio = {"):]
        exported_block = exported_block[: exported_block.index("};")]
        exported = set(re.findall(r"[A-Za-z_]\w*", exported_block)) - {"window", "PaiStudio"}
        called = set(re.findall(r"PaiStudio\.([A-Za-z_]\w*)", HTML))
        self.assertGreater(len(called), 5)
        self.assertEqual(called - exported, set())

    def test_every_view_has_a_container_and_the_router_knows_it(self):
        views = re.search(r"const VIEWS = \[([^\]]+)\]", HTML).group(1)
        names = re.findall(r"'([a-z]+)'", views)
        self.assertEqual(names, ["welcome", "studio", "projects", "editor", "outputs", "inputs", "brand"])
        for name in names:
            self.assertIn(f'id="view-{name}"', HTML)
        for name in names:
            if name != "editor":   # the editor lives under the Projects nav entry
                self.assertIn(f'id="btn-tab-{name}"', HTML)

    def test_the_page_uses_the_external_assets_and_no_inline_stylesheet(self):
        self.assertIn('href="/static/css/studio.css"', HTML)
        self.assertIn('src="/static/js/studio.js"', HTML)
        self.assertNotIn("<style", HTML)

    def test_theme_is_set_before_first_paint(self):
        head = HTML[: HTML.index("</head>")]
        self.assertIn("data-theme", head)
        self.assertLess(head.index("pai_theme"), head.index("studio.css"))

    def test_no_hard_coded_light_colours_left_in_inline_styles(self):
        inline = re.findall(r'style="([^"]*)"', HTML)
        offenders = [s for s in inline if re.search(r"background\s*:\s*(#fff\b|#ffffff|#f8fafc|#f1f5f9|white)", s)]
        self.assertEqual(offenders, [])


class CssTests(unittest.TestCase):
    def test_dark_palette_is_defined_once(self):
        self.assertEqual(CSS.count(':root[data-theme="dark"] {'), 1)

    def test_every_class_the_template_uses_for_layout_is_styled(self):
        classes = set()
        for value in re.findall(r'class="([^"]+)"', HTML):
            classes.update(value.split())
        # classes that are only JS hooks / state flags, or styled through other selectors
        hooks = {"active", "checked", "info", "success", "warning", "hidden", "open", "completed", "done", "gold",
                 "seg-outputs", "seg-movies", "seg-temp", "theme-icon-sun", "theme-icon-moon"}
        missing = sorted(c for c in classes - hooks if not re.search(r"\." + re.escape(c) + r"(?![\w-])", CSS))
        self.assertEqual(missing, [], "classes used in index.html without any CSS rule")

    def test_classes_created_by_the_script_are_styled(self):
        classes = set()
        for value in re.findall(r"class: '([^']+)'", JS):
            classes.update(value.split())
        hooks = {"info", "success", "se-empty"}   # se-empty only marks the "no matches" placeholder
        missing = sorted(c for c in classes - hooks if not re.search(r"\." + re.escape(c) + r"(?![\w-])", CSS))
        self.assertEqual(missing, [])


class ServedTests(unittest.TestCase):
    def test_static_files_are_served(self):
        import web_ui
        from fastapi.testclient import TestClient
        client = TestClient(web_ui.app)
        css = client.get("/static/css/studio.css")
        js = client.get("/static/js/studio.js")
        page = client.get("/")
        self.assertEqual((css.status_code, js.status_code, page.status_code), (200, 200, 200))
        self.assertIn("text/css", css.headers["content-type"])
        self.assertIn("javascript", js.headers["content-type"])
        self.assertIn("/static/css/studio.css", page.text)

    def test_static_is_reachable_without_login_but_the_api_is_not(self):
        from unittest.mock import patch
        import web_ui
        from fastapi.testclient import TestClient
        with patch.dict(os.environ, {"WEB_UI_PASSWORD": "s3cret"}):
            client = TestClient(web_ui.app)
            self.assertEqual(client.get("/static/css/studio.css").status_code, 200)
            self.assertEqual(client.get("/api/projects").status_code, 401)


if __name__ == "__main__":
    unittest.main()
