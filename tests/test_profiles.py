"""services.profiles - channel profiles validated against the option model."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import profiles as pf  # noqa: E402


class ProfileCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pai_profiles_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.file = os.path.join(self.tmp, "profiles.json")
        env = patch.dict(os.environ, {"PAI_PROFILES_FILE": self.file})
        env.start()
        self.addCleanup(env.stop)

    GOOD = {"language": "burmese_nilar", "subtitle_style": "yellow_pop", "watermark_text": "My Channel",
            "watermark_opacity": 0.6, "outro_card": True, "sfx_volume": 0.2}


class CrudTests(ProfileCase):
    def test_create_list_get_update_delete(self):
        self.assertEqual(pf.list_profiles(), [])
        made = pf.save_profile("  My   Channel ", "recap", self.GOOD)
        self.assertEqual(made["name"], "My Channel")
        self.assertEqual(made["engine_mode"], "recap")
        self.assertEqual(made["options"], self.GOOD)
        self.assertEqual(pf.get_profile(made["id"]), made)
        self.assertEqual([p["id"] for p in pf.list_profiles()], [made["id"]])

        updated = pf.save_profile("My Channel (new)", "recap", {"language": "english"}, profile_id=made["id"])
        self.assertEqual(updated["id"], made["id"])
        self.assertEqual(updated["created_at"], made["created_at"])
        self.assertEqual(updated["options"], {"language": "english"})
        self.assertEqual(len(pf.list_profiles()), 1)

        pf.delete_profile(made["id"])
        self.assertEqual(pf.list_profiles(), [])

    def test_file_is_readable_json_and_survives_burmese_names(self):
        pf.save_profile("ကျွန်တော့်ချန်နယ်", "hardsub", {"subtitle_style": "box_black"})
        with open(self.file, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["profiles"][0]["name"], "ကျွန်တော့်ချန်နယ်")
        self.assertFalse(os.path.exists(self.file + ".tmp"))

    def test_names_are_unique_ignoring_case_but_a_profile_may_keep_its_own_name(self):
        a = pf.save_profile("Alpha", "recap", {})
        with self.assertRaises(pf.ProfileError) as caught:
            pf.save_profile("alpha", "recap", {})
        self.assertEqual(caught.exception.code, "duplicate")
        pf.save_profile("Alpha", "recap", {"language": "english"}, profile_id=a["id"])   # same profile: fine

    def test_unknown_id_and_missing_profile(self):
        for call in (lambda: pf.get_profile("nope"), lambda: pf.delete_profile("nope"),
                     lambda: pf.save_profile("x", "recap", {}, profile_id="nope")):
            with self.assertRaises(pf.ProfileError) as caught:
                call()
            self.assertEqual(caught.exception.code, "not_found")

    def test_profile_limit(self):
        for i in range(pf.MAX_PROFILES):
            pf.save_profile(f"p{i}", "recap", {})
        with self.assertRaises(pf.ProfileError) as caught:
            pf.save_profile("one too many", "recap", {})
        self.assertEqual(caught.exception.code, "limit")

    def test_corrupt_file_is_treated_as_empty(self):
        with open(self.file, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertEqual(pf.list_profiles(), [])
        pf.save_profile("fresh", "recap", {})
        self.assertEqual(len(pf.list_profiles()), 1)


class ValidationTests(ProfileCase):
    def assertRejected(self, engine, options, fragment):
        with self.assertRaises(pf.ProfileError) as caught:
            pf.save_profile("x", engine, options)
        self.assertIn(fragment, str(caught.exception))

    def test_unknown_options_are_rejected(self):
        self.assertRejected("recap", {"gemini_api_key": "secret"}, "Unknown option")
        self.assertRejected("recap", {"__proto__": 1}, "Unknown option")

    def test_per_video_options_are_rejected(self):
        for name in ("input", "project_name", "custom_thumb_title", "trim_end", "context_hint", "resume", "engine_mode"):
            with self.subTest(option=name):
                self.assertRejected("recap", {name: "x"}, "single video")

    def test_options_must_apply_to_the_engine(self):
        self.assertRejected("hardsub", {"outro_card": True}, "does not apply")
        self.assertRejected("subtitle", {"tts_voice": "x"}, "does not apply")

    def test_value_types_are_enforced(self):
        self.assertRejected("recap", {"outro_card": "yes"}, "bool")
        self.assertRejected("recap", {"sfx_volume": "loud"}, "float")
        self.assertRejected("recap", {"sfx_volume": True}, "float")
        self.assertRejected("recap", {"watermark_text": 5}, "str")
        self.assertRejected("recap", {"stage_toggles": "all"}, "dict")
        self.assertRejected("recap", ["not", "a", "dict"], "object")

    def test_null_means_default_and_numbers_may_be_ints(self):
        made = pf.save_profile("ok", "recap", {"watermark_text": None, "sfx_volume": 1, "stage_toggles": {"qa": False}})
        self.assertEqual(made["options"]["stage_toggles"], {"qa": False})

    def test_name_and_engine_rules(self):
        for bad in ("", "   ", None, "x" * (pf.MAX_NAME_CHARS + 1)):
            with self.subTest(name=bad):
                with self.assertRaises(pf.ProfileError):
                    pf.save_profile(bad, "recap", {})
        for engine in ("download", "bogus", "", None):
            with self.subTest(engine=engine):
                with self.assertRaises(pf.ProfileError):
                    pf.save_profile("x", engine, {})

    def test_oversized_text_values(self):
        self.assertRejected("recap", {"watermark_text": "x" * 501}, "too long")

    def test_nothing_is_written_when_validation_fails(self):
        with self.assertRaises(pf.ProfileError):
            pf.save_profile("x", "recap", {"nope": 1})
        self.assertFalse(os.path.exists(self.file))

    def test_every_allowed_option_really_exists_in_the_option_model(self):
        """Guards against the option model renaming something a stored profile still uses."""
        from core.options import BY_NAME
        self.assertTrue(set(self.GOOD) <= set(BY_NAME))
        self.assertTrue(pf.PER_VIDEO_OPTIONS <= set(BY_NAME))



class DriveStorageTests(unittest.TestCase):
    """On Colab the profiles live on Google Drive next to config.json, so a runtime reset keeps them."""

    def test_drive_folder_is_used_when_it_exists_and_no_override_is_set(self):
        tmp = tempfile.mkdtemp(prefix="pai_drive_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        env = {k: v for k, v in os.environ.items() if k != "PAI_PROFILES_FILE"}
        with patch.dict(os.environ, env, clear=True), patch.object(pf, "DRIVE_DIR", tmp):
            self.assertEqual(pf._path(), os.path.join(tmp, "profiles.json"))
            pf.save_profile("Colab Channel", "recap", {"language": "burmese"})
            self.assertTrue(os.path.exists(os.path.join(tmp, "profiles.json")))
            self.assertEqual([p["name"] for p in pf.list_profiles()], ["Colab Channel"])

    def test_local_file_without_drive_and_override_wins(self):
        env = {k: v for k, v in os.environ.items() if k != "PAI_PROFILES_FILE"}
        with patch.dict(os.environ, env, clear=True), patch.object(pf, "DRIVE_DIR", os.path.join(tempfile.gettempdir(), "no-such-drive")):
            self.assertEqual(pf._path(), "profiles.json")
        with patch.dict(os.environ, {"PAI_PROFILES_FILE": "/x/custom.json"}), patch.object(pf, "DRIVE_DIR", tempfile.gettempdir()):
            self.assertEqual(pf._path(), "/x/custom.json")


if __name__ == "__main__":
    unittest.main()
