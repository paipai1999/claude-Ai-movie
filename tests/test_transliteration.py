import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from brain.burmese_utils import (
    replace_numbers_with_burmese,
    transliterate_english_acronyms,
    num_to_burmese,
    myanmar_digits_to_arabic,
    convert_to_tts_phonetic_burmese,
    normalize_standard_burmese_spelling,
)

class TestTransliteration(unittest.TestCase):

    def test_number_conversion(self):
        """Verify digits are converted to colloquial spoken Burmese."""
        self.assertEqual(num_to_burmese(0), "သုည")
        self.assertEqual(num_to_burmese(1), "တစ်")
        self.assertEqual(num_to_burmese(10), "ဆယ်")
        self.assertEqual(num_to_burmese(15), "ဆယ့်ငါး")
        self.assertEqual(num_to_burmese(100), "တစ်ရာ")

    def test_replace_numbers_in_sentence(self):
        """Verify inline numbers are translated into natural Burmese words."""
        text = "သူက အခန်း 105 မှာ 2.5 နာရီကြာ နေခဲ့တယ်"
        res = replace_numbers_with_burmese(text)
        self.assertNotIn("105", res)
        self.assertNotIn("2.5", res)
        self.assertIn("ဒသမ", res)

    def test_common_acronyms_transliteration(self):
        """Verify common acronyms are transliterated phonetically."""
        self.assertEqual(transliterate_english_acronyms("CCTV"), "စီစီတီဗီ")
        self.assertEqual(transliterate_english_acronyms("VIP"), "ဗွီအိုင်ပီ")
        self.assertEqual(transliterate_english_acronyms("Doctor"), "ဒေါက်တာ")
        self.assertEqual(transliterate_english_acronyms("AI"), "အေအိုင်")
        self.assertEqual(transliterate_english_acronyms("CEO"), "စီအီးအို")

    def test_character_names_transliteration(self):
        """Verify English character names are transliterated without disappearing."""
        names_sentence = "Arthur told Jack and Parker about the Titan Caldwell project."
        res = transliterate_english_acronyms(names_sentence)
        self.assertIn("အာသာ", res)
        self.assertIn("ဂျက်ခ်", res)
        self.assertIn("ပါကာ", res)
        self.assertIn("တိုက်တန်", res)
        self.assertIn("ကောလ်ဝဲလ်", res)

    def test_myanmar_digits_normalization(self):
        """Verify Myanmar digits are standardized to Arabic before word conversion."""
        self.assertEqual(myanmar_digits_to_arabic("၁၂၃"), "123")
        self.assertEqual(myanmar_digits_to_arabic("၀၄၅"), "045")

    def test_convert_to_tts_phonetic_burmese(self):
        """Verify TTS text is converted to natural spoken phonetics without glottal stuttering."""
        formal_text = "ကျွန်တော် တစ်ယောက်တည်း သွားမယ်။ ဥက္ကဋ္ဌကြီးက အံ့ဩသွားပြီး သမ္မတကြီးကို မေတ္တာရပ်ခံခဲ့တယ်။"
        phonetic = convert_to_tts_phonetic_burmese(formal_text)
        self.assertIn("ကျနော်", phonetic)
        self.assertIn("တယောက်", phonetic)
        self.assertIn("အုတ်ကထ", phonetic)
        self.assertIn("အံ့အော", phonetic)
        self.assertIn("သမ်မတ", phonetic)
        self.assertIn("မိတ်တာ", phonetic)
        self.assertNotIn("ကျွန်တော်", phonetic)
        self.assertNotIn("ဥက္ကဋ္ဌ", phonetic)

    def test_normalize_standard_burmese_spelling(self):
        """Verify spoken phonetic text is converted to 100% correct standard Myanmar orthography for subtitles."""
        spoken_text = "ကျနော် တယောက်တည်း သွားမယ်။ ဥက္ကဌကြီးက အံ့သြသွားပြီး ဝတ်ထုကို ဖတ်တယ်။ တကယ်တော့ သူက ယောင်္ကျားကောင်းပါ။"
        standard = normalize_standard_burmese_spelling(spoken_text)
        self.assertIn("ကျွန်တော်", standard)
        self.assertIn("တစ်ယောက်", standard)
        self.assertIn("ဥက္ကဋ္ဌ", standard)
        self.assertIn("အံ့ဩ", standard)
        self.assertIn("ဝတ္ထု", standard)
        self.assertIn("ယောကျ်ား", standard)
        self.assertIn("တကယ်တော့", standard)  # Non-classifier 'တ' must be preserved!
        self.assertNotIn("ကျနော်", standard)
        self.assertNotIn("ဥက္ကဌ", standard)
        self.assertNotIn("အံ့သြ", standard)

    def test_voice_agent_applies_tts_phonetics(self):
        """Verify VoiceAgent._prepare_tts_text converts text phonetically for Edge TTS."""
        from agents.voice_agent import VoiceAgent
        agent = VoiceAgent(voice="my-MM-ThihaNeural")
        raw_text = "ကျွန်တော် တစ်ယောက်တည်း သွားပါတယ်။"
        prepared = agent._prepare_tts_text(raw_text)
        self.assertIn("ကျနော်", prepared)
        self.assertIn("တယောက်", prepared)

    def test_subtitle_builder_applies_standard_orthography(self):
        """Verify subtitle builders normalize text to standard Myanmar orthography."""
        from core.subtitle_builder import build_srt_script, build_ass_script
        segments = [
            {"start_s": 0.0, "end_s": 3.0, "burmese": "ကျနော် တယောက်တည်း သွားမယ်။ ဥက္ကဌကြီးက အံ့သြသွားတယ်။"}
        ]
        srt_out = build_srt_script(segments)
        ass_out = build_ass_script(segments)

        self.assertIn("ကျွန်တော်", srt_out)
        self.assertIn("တစ်ယောက်", srt_out)
        self.assertIn("ဥက္ကဋ္ဌ", srt_out)
        self.assertIn("အံ့ဩ", srt_out)
        self.assertNotIn("ကျနော်", srt_out)

        self.assertIn("ကျွန်တော်", ass_out)
        self.assertIn("တစ်ယောက်", ass_out)
        self.assertIn("ဥက္ကဋ္ဌ", ass_out)
        self.assertIn("အံ့ဩ", ass_out)
        self.assertNotIn("ကျနော်", ass_out)

if __name__ == "__main__":
    unittest.main()

