"""core.text_compare - comparison helpers must not delete combining marks (Myanmar, Thai, Devanagari)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.text_compare import normalize, squash, trigram_similarity  # noqa: E402


class NormalizeTests(unittest.TestCase):
    def test_keeps_myanmar_vowel_signs_and_medials(self):
        # A plain ``\W`` regex would turn "ကျေးဇူး" into "က ဇ" - the signs are combining marks.
        self.assertEqual(normalize("ကျေးဇူး"), "ကျေးဇူး")
        self.assertEqual(squash("ကျေးဇူး တင်ပါတယ်။"), "ကျေးဇူးတင်ပါတယ်")

    def test_keeps_thai_and_devanagari_marks(self):
        self.assertEqual(normalize("สวัสดีครับ"), "สวัสดีครับ")
        self.assertEqual(normalize("नमस्ते!"), "नमस्ते")

    def test_drops_punctuation_symbols_and_extra_whitespace(self):
        self.assertEqual(normalize("  Hello,   WORLD!! "), "hello world")
        self.assertEqual(normalize("“Quoted” — text…"), "quoted text")
        self.assertEqual(normalize(None), "")

    def test_keeps_digits(self):
        self.assertEqual(normalize("Room 101."), "room 101")


class SimilarityTests(unittest.TestCase):
    def test_marks_matter(self):
        # Same consonants, different vowel signs: must not look identical.
        self.assertLess(trigram_similarity("ကျေးဇူးတင်ပါတယ်", "ကျားဇူးတင်ပိတယ်"), 1.0)

    def test_spacing_and_punctuation_are_noise(self):
        self.assertEqual(trigram_similarity("ကျေးဇူး တင်ပါတယ်။", "ကျေးဇူးတင်ပါတယ်"), 1.0)


if __name__ == "__main__":
    unittest.main()
