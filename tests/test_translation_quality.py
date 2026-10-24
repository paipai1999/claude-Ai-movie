"""core.translation_quality - offline validators for a batch of translated narration lines."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import translation_quality as tq  # noqa: E402

GOOD_1 = "ဒီဇာတ်လမ်းမှာတော့ သူရဲကောင်းက မြို့ထဲကို ပြန်လာခဲ့တာပေါ့ဗျာ"
GOOD_2 = "အဲဒီအချိန်မှာပဲ တံခါးကို ဝုန်းခနဲ ဖွင့်ဝင်လာတဲ့ လူတစ်ယောက်ကို တွေ့လိုက်ရပါတယ်"
GOOD_3 = "မထင်မှတ်ထားဘဲ သူ့ညီမလေးက ပြတင်းပေါက်ကနေ ချောင်းကြည့်နေတာကို သိလိုက်ရတယ်"


def codes(problems):
    return sorted((p.seg_id, p.code) for p in problems)


class FindProblemsTests(unittest.TestCase):
    def test_clean_burmese_batch_has_no_problems(self):
        self.assertEqual(tq.find_problems({1: GOOD_1, 2: GOOD_2, 3: GOOD_3}), [])

    def test_missing_and_placeholder_lines(self):
        found = tq.find_problems({1: None, 2: "   ", 3: "...", 4: "N/A", 5: "[translation]", 6: GOOD_1})
        self.assertEqual(codes(found), [(1, "missing"), (2, "missing"), (3, "placeholder"), (4, "placeholder"), (5, "placeholder")])

    def test_chatter_instead_of_narration(self):
        found = tq.find_problems({1: "Here is the translation: " + GOOD_1, 2: "(Note: " + GOOD_2 + ")", 3: "```json " + GOOD_3})
        self.assertEqual({p.code for p in found if p.code == "meta_text"}, {"meta_text"})
        self.assertEqual({p.seg_id for p in found if p.code == "meta_text"}, {1, 2, 3})

    def test_english_or_chinese_line_is_untranslated(self):
        found = tq.find_problems({1: "I will find you wherever you hide.", 2: "我一定会找到你", 3: GOOD_1})
        self.assertEqual(codes(found), [(1, "untranslated"), (2, "untranslated")])

    def test_foreign_script_leaking_into_a_burmese_line(self):
        found = tq.find_problems({1: GOOD_1 + " 你好", 2: GOOD_2 + " привет"})
        self.assertEqual(codes(found), [(1, "foreign_script"), (2, "foreign_script")])

    def test_loose_english_words_are_flagged_but_acronyms_and_one_brand_are_fine(self):
        self.assertEqual(tq.find_problems({1: GOOD_1 + " CCTV", 2: GOOD_2 + " iPhone"}), [])
        found = tq.find_problems({1: GOOD_1 + " Pai Studio Movie"})
        self.assertEqual(codes(found), [(1, "latin_words")])
        self.assertIn("Pai", found[0].detail)

    def test_copied_source_text(self):
        found = tq.find_problems({1: "Run, run for your life!"}, source={1: "run, run for your life"}, burmese_target=False)
        self.assertEqual(codes(found), [(1, "copied_source")])

    def test_non_burmese_target_skips_script_checks(self):
        self.assertEqual(tq.find_problems({1: "Run for your life, quickly!"}, burmese_target=False), [])

    def test_adjacent_near_duplicates_are_flagged(self):
        found = tq.find_problems({1: GOOD_1, 2: GOOD_1 + "။"}, source={1: "line one", 2: "line two"})
        self.assertEqual(codes(found), [(2, "duplicate")])

    def test_identical_sources_may_have_identical_narration(self):
        self.assertEqual(
            tq.find_problems({1: GOOD_1, 2: GOOD_1}, source={1: "Come back!", 2: "Come back!"}), []
        )

    def test_duplicate_against_previous_tail_context(self):
        found = tq.find_problems({5: GOOD_1}, previous_tail=GOOD_1, source={5: "x"})
        self.assertEqual(codes(found), [(5, "duplicate")])

    def test_model_restating_the_previous_line_at_the_start(self):
        previous = GOOD_1
        echoing = previous[-20:] + " ပြီးတော့ " + GOOD_3
        found = tq.find_problems({2: echoing}, previous_tail=previous, source={2: "x"})
        self.assertEqual(codes(found), [(2, "repeat_boundary")])

    def test_over_budget_only_beyond_the_ratio(self):
        text = GOOD_2   # around 70 characters
        limit = int(len(text) / 1.30)   # 1.3x over - tolerated (the voice can stretch a little)
        self.assertEqual(tq.find_problems({1: text}, max_chars={1: limit}), [])
        found = tq.find_problems({1: text}, max_chars={1: int(len(text) / 1.7)})
        self.assertEqual(codes(found), [(1, "over_budget")])
        self.assertIn("limit", found[0].detail)

    def test_short_repeated_lines_are_not_duplicates(self):
        self.assertEqual(tq.find_problems({1: "ဟုတ်ကဲ့ပါ", 2: "ဟုတ်ကဲ့ပါ"}, source={1: "yes", 2: "right"}), [])

    def test_results_follow_id_order(self):
        found = tq.find_problems({3: None, 1: None, 2: None})
        self.assertEqual([p.seg_id for p in found], [1, 2, 3])


class FeedbackTests(unittest.TestCase):
    def test_feedback_lists_each_affected_line_with_all_its_problems(self):
        problems = tq.find_problems({1: "I will find you again.", 2: None, 3: GOOD_1})
        text = tq.feedback_text(problems)
        self.assertEqual(len(text.splitlines()), 2)
        self.assertTrue(text.splitlines()[0].startswith("- id 1:"))
        self.assertIn("Burmese", text.splitlines()[0])
        self.assertTrue(text.splitlines()[1].startswith("- id 2:"))

    def test_advice_includes_detail_when_known(self):
        problem = tq.Problem(4, "over_budget", "120 characters, limit 60")
        self.assertIn("120 characters, limit 60", problem.advice())
        self.assertNotIn("{detail}", tq.Problem(4, "latin_words").advice())

    def test_helpers(self):
        problems = tq.find_problems({1: None, 2: GOOD_1, 3: None})
        self.assertEqual(tq.ids_with_problems(problems), {1, 3})
        self.assertEqual(set(tq.group_by_id(problems)), {1, 3})

    def test_every_code_has_advice(self):
        for code in ("missing", "placeholder", "meta_text", "untranslated", "foreign_script", "latin_words",
                     "copied_source", "duplicate", "repeat_boundary", "over_budget"):
            self.assertNotEqual(tq.Problem(1, code).advice(), code)


class SimilarityTests(unittest.TestCase):
    def test_identical_and_unrelated(self):
        self.assertEqual(tq.trigram_similarity(GOOD_1, GOOD_1), 1.0)
        self.assertLess(tq.trigram_similarity(GOOD_1, GOOD_2), 0.3)

    def test_ignores_case_punctuation_and_spacing(self):
        self.assertEqual(tq.trigram_similarity("Hello, World!!", "hello world"), 1.0)

    def test_very_short_text(self):
        self.assertEqual(tq.trigram_similarity("ab", "ab"), 1.0)
        self.assertEqual(tq.trigram_similarity("ab", "cd"), 0.0)
        self.assertEqual(tq.trigram_similarity("", ""), 0.0)


if __name__ == "__main__":
    unittest.main()
