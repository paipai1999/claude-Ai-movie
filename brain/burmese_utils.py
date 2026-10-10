import re

# Standard Myanmar digits and spoken words
DIGITS = ['', 'တစ်', 'နှစ်', 'သုံး', 'လေး', 'ငါး', 'ခြောက်', 'ခုနစ်', 'ရှစ်', 'ကိုး']
DIGIT_MAP = {
    '0': 'သုည', '1': 'တစ်', '2': 'နှစ်', '3': 'သုံး', '4': 'လေး',
    '5': 'ငါး', '6': 'ခြောက်', '7': 'ခုနစ်', '8': 'ရှစ်', '9': 'ကိုး'
}

# Common English movie/drama acronyms and terms -> Burmese phonetics
COMMON_ACRONYMS_MM = {
    r'\bJack\b': 'ဂျက်ခ်',
    r'\bJohn\b': 'ဂျွန်',
    r'\bArthur\b': 'အာသာ',
    r'\bParker\b': 'ပါကာ',
    r'\bMike\b': 'မိုက်ခ်',
    r'\bTom\b': 'တွမ်',
    r'\bSam\b': 'ဆမ်',
    r'\bSarah\b': 'ဆာရာ',
    r'\bAnna\b': 'အန်နာ',
    r'\bAlex\b': 'အဲလက်စ်',
    r'\bMax\b': 'မက်စ်',
    r'\bChris\b': 'ခရစ်',
    r'\bDavid\b': 'ဒေးဗစ်',
    r'\bPeter\b': 'ပီတာ',
    r'\bTony\b': 'တိုနီ',
    r'\bTitan\b': 'တိုက်တန်',
    r'\bCaldwell\b': 'ကောလ်ဝဲလ်',
    r'\bMary\b': 'မေရီ',
    r'\bJames\b': 'ဂျိမ်းစ်',
    r'\bGeorge\b': 'ဂျော့ဂျ်',
    r'\bPaul\b': 'ပေါလ်',
    r'\bMark\b': 'မာ့ခ်',
    r'\bKevin\b': 'ကယ်ဗင်',
    r'\bRyan\b': 'ရိုင်ယန်',
    r'\bEthan\b': 'အီသန်',
    r'\bLeo\b': 'လီယို',
    r'\bDaniel\b': 'ဒန်နီရယ်',
    r'\bHarry\b': 'ဟယ်ရီ',
    r'\bCCTV\b': 'စီစီတီဗီ',
    r'\bVIP\b': 'ဗွီအိုင်ပီ',
    r'\bVVIP\b': 'ဗွီဗွီအိုင်ပီ',
    r'\bFBI\b': 'အက်ဖ်ဘီအိုင်',
    r'\bCIA\b': 'စီအိုင်အေ',
    r'\bCEO\b': 'စီအီးအို',
    r'\bAI\b': 'အေအိုင်',
    r'\bOK\b': 'အိုကေ',
    r'\bO\.K\.\b': 'အိုကေ',
    r'\bUSB\b': 'ယူအက်စ်ဘီ',
    r'\bSIM\b': 'ဆင်းမ်',
    r'\bGPS\b': 'ဂျီပီအက်စ်',
    r'\bTV\b': 'တီဗီ',
    r'\bPC\b': 'ပီစီ',
    r'\biPhone\b': 'အိုင်ဖုန်း',
    r'\biPad\b': 'အိုင်ပတ်',
    r'\bID\b': 'အိုင်ဒီ',
    r'\bSOS\b': 'အက်စ်အိုအက်စ်',
    r'\bPDF\b': 'ပီဒီအက်ဖ်',
    r'\bApp\b': 'အက်ပ်',
    r'\bApps\b': 'အက်ပ်များ',
    r'\bWifi\b': 'ဝိုင်ဖိုင်',
    r'\bWi-Fi\b': 'ဝိုင်ဖိုင်',
    r'\bDoctor\b': 'ဒေါက်တာ',
    r'\bDr\.\b': 'ဒေါက်တာ',
    r'\bBoss\b': 'ဘော့စ်',
    r'\bHello\b': 'ဟယ်လို',
    r'\bHi\b': 'ဟိုင်း',
    r'\bHey\b': 'ဟေး',
    r'\bBye\b': 'တာ့တာ',
    r'\bBye-bye\b': 'ဘိုင်ဘိုင်း',
    r'\bDNA\b': 'ဒီအင်န်အေ',
    r'\bATM\b': 'အေတီအမ်',
    r'\bKTV\b': 'ကေတီဗီ',
    r'\bDJ\b': 'ဒီဂျေ',
    r'\bBMW\b': 'ဘီအမ်ဒဗလျူ',
    r'\bSUV\b': 'အက်စ်ယူဗီ',
    r'\bUFO\b': 'ယူအက်ဖ်အို',
    r'\bFacebook\b': 'ဖေ့စ်ဘွတ်ခ်',
    r'\bTikTok\b': 'တစ်တော့ခ်',
    r'\bYouTube\b': 'ယူကျုဘ်',
    r'\bSMS\b': 'မက်ဆေ့ခ်ျ',
    r'\bOT\b': 'အိုတီ',
    r'\bOP\b': 'အိုပီ',
    r'\bPUBG\b': 'ပတ်ဂျီ',
    r'\bNASA\b': 'နာဆာ',
    r'\bSWAT\b': 'ဆွတ်တ်',
    r'\bSir\b': 'ဆာ',
    r'\bMadam\b': 'မဒမ်',
    r'\bMr\.\b': 'မစ္စတာ',
    r'\bMrs\.\b': 'မစ္စစ်',
    r'\bMiss\b': 'မစ်',
}

# Individual phonetic letter transliterations for remaining uppercase acronyms
EN_LETTER_TO_MM = {
    'A': 'အေ', 'B': 'ဘီ', 'C': 'စီ', 'D': 'ဒီ', 'E': 'အီး',
    'F': 'အက်ဖ်', 'G': 'ဂျီ', 'H': 'အိတ်ခ်ျ', 'I': 'အိုင်', 'J': 'ဂျေ',
    'K': 'ကေ', 'L': 'အယ်လ်', 'M': 'အမ်', 'N': 'အန်', 'O': 'အို',
    'P': 'ပီ', 'Q': 'ကျူ', 'R': 'အာရ်', 'S': 'အက်စ်', 'T': 'တီ',
    'U': 'ယူ', 'V': 'ဗွီ', 'W': 'ဒဗလျူ', 'X': 'အက်စ်', 'Y': 'ဝိုင်', 'Z': 'ဇက်'
}


def num_to_burmese(num: int) -> str:
    """
    Converts an integer to natural colloquial Burmese spoken text.
    Handles everyday spoken patterns:
      10 -> ဆယ်
      11 -> ဆယ့်တစ်, 15 -> ဆယ့်ငါး
      20 -> နှစ်ဆယ်, 25 -> နှစ်ဆယ့်ငါး
      100 -> တစ်ရာ, 105 -> တစ်ရာ့ငါး, 125 -> တစ်ရာ့နှစ်ဆယ့်ငါး
      1000 -> တစ်ထောင်, 2026 -> နှစ်ထောင့်နှစ်ဆယ့်ခြောက်
      100,000 -> တစ်သိန်း
    """
    if num == 0:
        return 'သုည'
    if num < 0:
        return 'အနှုတ် ' + num_to_burmese(-num)

    if num >= 1_000_000:
        millions = num // 1_000_000
        rem = num % 1_000_000
        res = num_to_burmese(millions) + 'သန်း'
        if rem > 0:
            res += ' ' + num_to_burmese(rem)
        return res

    # Single digit
    if num < 10:
        return DIGITS[num]

    # Exactly 10
    if num == 10:
        return 'ဆယ်'

    # Teens: 11 - 19
    if 11 <= num <= 19:
        return 'ဆယ့်' + DIGITS[num - 10]

    # Tens: 20 - 99
    if 20 <= num < 100:
        tens = num // 10
        units = num % 10
        if units == 0:
            return DIGITS[tens] + 'ဆယ်'
        return DIGITS[tens] + 'ဆယ့်' + DIGITS[units]

    # Hundreds: 100 - 999
    if num < 1000:
        h = num // 100
        rem = num % 100
        prefix = DIGITS[h] + ('ရာ' if rem == 0 else 'ရာ့')
        return prefix + (num_to_burmese(rem) if rem > 0 else '')

    # Thousands: 1,000 - 9,999
    if num < 10000:
        th = num // 1000
        rem = num % 1000
        prefix = DIGITS[th] + ('ထောင်' if rem == 0 else 'ထောင့်')
        return prefix + (num_to_burmese(rem) if rem > 0 else '')

    # Ten-thousands: 10,000 - 99,999 (သောင်း)
    if num < 100000:
        tt = num // 10000
        rem = num % 10000
        prefix = DIGITS[tt] + 'သောင်း'
        return prefix + (' ' + num_to_burmese(rem) if rem > 0 else '')

    # Lakh / Hundred-thousands: 100,000 - 999,999 (သိန်း)
    if num < 1000000:
        lakh = num // 100000
        rem = num % 100000
        prefix = num_to_burmese(lakh) + 'သိန်း'
        return prefix + (' ' + num_to_burmese(rem) if rem > 0 else '')

    return str(num)


def myanmar_digits_to_arabic(text: str) -> str:
    """Converts Myanmar digits (၀-၉) to Arabic (0-9) to standardize."""
    mm_digits = '၀၁၂၃၄၅၆၇၈၉'
    en_digits = '0123456789'
    trans = str.maketrans(mm_digits, en_digits)
    return text.translate(trans)


def replace_numbers_with_burmese(text: str) -> str:
    """
    Finds all numbers (integers and decimals) in a string and replaces
    them with natural Burmese spoken words.
    e.g. '2.5' -> 'နှစ် ဒသမ ငါး'
         '15'  -> 'ဆယ့်ငါး'
         '1,500' -> 'တစ်ထောင့်ငါးရာ'
    """
    if not text:
        return ""

    text = myanmar_digits_to_arabic(str(text))

    def replacer(match):
        val = match.group(0).replace(",", "")
        # Decimal numbers (e.g. 2.5, 0.05)
        if "." in val:
            parts = val.split(".")
            int_part = int(parts[0]) if parts[0] else 0
            frac_digits = ''.join(DIGIT_MAP.get(d, d) for d in parts[1])
            int_words = 'သုည' if int_part == 0 else num_to_burmese(int_part)
            return f"{int_words} ဒသမ {frac_digits}"

        try:
            return num_to_burmese(int(val))
        except ValueError:
            return match.group(0)

    # Match floats first (e.g. 1,000.50 or 2.5), then plain integers with commas or digits
    pattern = re.compile(r'\d+(?:,\d+)*\.\d+|\d{1,3}(?:,\d{3})+|\d+')
    return pattern.sub(replacer, text)


def transliterate_english_acronyms(text: str) -> str:
    """
    Transliterates English acronyms, abbreviations, and common movie terms into
    natural Burmese phonetics so TTS reads them aloud clearly without skipping.
    e.g. 'CCTV' -> 'စီစီတီဗီ'
         'VIP'  -> 'ဗွီအိုင်ပီ'
         'CEO'  -> 'စီအီးအို'
    """
    if not text:
        return ""

    # 1. Map known terms and acronyms (case-insensitive)
    for pattern, replacement in COMMON_ACRONYMS_MM.items():
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)

    # 2. Phonetic transliteration for any remaining uppercase acronyms (e.g. BMW, KTV, XYZ)
    def letter_replacer(m):
        chars = [EN_LETTER_TO_MM.get(c.upper(), c) for c in m.group(0)]
        return ''.join(chars)

    text = re.sub(r'\b[A-Z]{2,6}\b', letter_replacer, text)
    return text


def sanitize_burmese_narration(text: str) -> str:
    """
    Sanitizes Burmese narration text to eliminate foreign character leakage,
    tokenizer glitches, and punctuation anomalies:
    1. Replaces known homoglyphs (e.g. Georgian 'კ' -> Myanmar 'က').
    2. Strips stray non-Burmese Unicode scripts (Cyrillic, Georgian, Greek, etc.)
       while strictly preserving Myanmar Unicode, ASCII alphanumeric, and punctuation.
    3. Normalizes Myanmar punctuation (dandas, commas, repeated symbols).
    """
    if not text:
        return ""

    s = str(text)

    # 1. Homoglyphs & lookalike correction
    homoglyphs = {
        '\u10d9': 'က',  # Georgian 'კ' -> Burmese 'က'
        '\u10e1': 'ဒ',  # Georgian 'ს' -> Burmese 'ဒ'
        '\u10eb': 'ဆ',  # Georgian 'ძ' -> Burmese 'ဆ'
        '\u043e': 'o',  # Cyrillic 'о'
        '\u0430': 'a',  # Cyrillic 'а'
    }
    for bad_ch, good_ch in homoglyphs.items():
        s = s.replace(bad_ch, good_ch)

    # 2. Filter stray foreign scripts outside Burmese, Latin, common digits & punctuation
    # Burmese Unicode range: \u1000-\u109F, \uAA60-\uAA7F, \uA9E0-\uA9FF
    # Punctuation & symbols: \u2000-\u206F (general punctuation), \uFE00-\uFE0F
    allowed_pattern = re.compile(
        r'[\u1000-\u109F\uAA60-\uAA7F\uA9E0-\uA9FF'  # Myanmar blocks
        r'a-zA-Z0-9'                                 # Latin alphanumeric
        r'\s'                                        # Whitespace
        r'.,!?:;\'"“”‘’()/\-_—%&@#$*+=\[\]{}~`|'     # Common punctuation
        r'\u200B\u200C\u200D'                        # Zero-width joiners/spaces
        r']+'
    )
    matches = allowed_pattern.findall(s)
    s = "".join(matches)

    # 3. Normalize punctuation anomalies
    s = re.sub(r'၊\s*၊+', '၊ ', s)
    s = re.sub(r'။\s*။+', '။ ', s)
    s = re.sub(r'၊\s*။', '။ ', s)
    s = re.sub(r'\s{2,}', ' ', s)

    # 4. Normalize dialogue persona particles (strip unnatural ရှင်/ရှင့် artifacts)
    s = sanitize_dialogue_persona_particles(s)

    return s.strip()


def sanitize_dialogue_persona_particles(text: str) -> str:
    """
    Sanitizes colloquial Myanmar dialogue particles to prevent robotic LLM artifacts:
    1. Removes robotic polite particles directly before or after exclamation marks and question marks
       (e.g., '! ခင်ဗျာ', '? ခင်ဗျာ', 'သေစမ်းပါ ခင်ဗျာ!', 'သွားစမ်းပါ ရှင့်!').
    2. Removes polite particles from negative commands/threats (e.g. 'မ...နဲ့ရှင်', 'မ...နဲ့ခင်ဗျာ' -> 'မ...နဲ့').
    3. Converts imperative polite particles to natural conversational particles (e.g. 'ထတော့ရှင်' -> 'ထတော့လေ').
    4. Strips 'ရှင်/ရှင့်' and 'ခင်ဗျာ/ဗျာ' from inner monologues or self-directed speech where speaker refers to themselves as 'ငါ'.
    """
    if not text:
        return ""
    s = str(text).strip()

    # 1. Robotic polite particles attached right after exclamation/question marks (e.g. "! ခင်ဗျာ", "?! ရှင့်")
    s = re.sub(r'([!?။၊]+)\s*(?:ခင်ဗျာ|ရှင့်|ဗျာ|ရှင်)\b', r'\1', s)

    # 2. Robotic polite particles attached right before exclamation marks (e.g. "သေစမ်း ခင်ဗျာ!", "မင်း ဘာထင်နေလဲ ခင်ဗျာ?!")
    s = re.sub(r'\s*(?:ခင်ဗျာ|ရှင့်)\s*([!]+)', r'\1', s)

    # 3. Negative commands/threats with ရှင် / ရှင့် / ခင်ဗျာ / ဗျာ (e.g. မလာနဲ့ရှင် -> မလာနဲ့, မလုပ်နဲ့ခင်ဗျာ -> မလုပ်နဲ့)
    s = re.sub(r'(မ[^\s။၊!?]+?နဲ့)\s*(?:ရှင်|ရှင့်|ခင်ဗျာ|ဗျာ)', r'\1', s)

    # 4. Imperatives ending with တော့ရှင် / တော့ခင်ဗျာ (e.g. ထတော့ရှင် -> ထတော့လေ, စားတော့ခင်ဗျာ -> စားတော့လေ)
    s = re.sub(r'([^\s။၊!?]+?တော့)\s*(?:ရှင်|ရှင့်|ခင်ဗျာ)', r'\1လေ', s)

    # 5. Inner monologue / self-talk with 'ငါ'
    if 'ငါ' in s:
        s = re.sub(r'လား\s*(?:ရှင်|ရှင့်|ခင်ဗျာ|ဗျာ)\s*([။၊!?]*)', r'လား\1', s)
        s = re.sub(r'(?:ပါရှင့်|ပါခင်ဗျာ|ပါဗျာ)\s*([။၊!?]*)', r'ပါ\1', s)
        s = re.sub(r'\s*(?:ရှင်|ရှင့်|ခင်ဗျာ|ဗျာ)\s*([။၊!?]*)$', r'\1', s)

    # 6. Normalize punctuation and double spaces
    s = re.sub(r'[ \t]+', ' ', s).strip()
    return s


def has_untranslated_foreign_script(text: str) -> bool:
    """
    Checks if a text segment contains untranslated foreign ideographs/alphabets
    such as Chinese (Hanzi), Japanese (Kana), or Korean (Hangul).
    """
    if not text:
        return False
    # Chinese Hanzi: \u4e00-\u9fff, Korean Hangul: \uac00-\ud7a3, Japanese: \u3040-\u30ff
    return bool(re.search(r'[\u4e00-\u9fff\uac00-\ud7a3\u3040-\u30ff]', str(text)))



def extract_clean_burmese_text(item) -> str:
    """
    Safely extracts clean Burmese text from LLM responses, dictionary objects, or raw strings.
    Prevents raw Python dictionary dumps (e.g. {'id': ..., 'burmese': ...}) from leaking into subtitles.
    Handles LLM key typos (e.g. 'buramese', 'translation', 'myanmar', 'text').
    """
    if not item:
        return ""

    if isinstance(item, dict):
        for k in ["burmese", "buramese", "translation", "myanmar", "burma", "text", "mm", "content"]:
            val = item.get(k)
            if val and isinstance(val, str) and val.strip():
                return val.strip()
        for val in item.values():
            if isinstance(val, str) and re.search(r"[\u1000-\u109F]", val):
                return val.strip()
        return ""

    s = str(item).strip()
    if s.startswith("{") and s.endswith("}"):
        m = re.search(r"['\"](?:burmese|buramese|translation|myanmar|text)['\"]\s*:\s*['\"]([^'\"]+)['\"]", s)
        if m:
            return m.group(1).strip()
        mm_m = re.search(r"['\"]([\u1000-\u109F\s၊။!?,.-]+)['\"]", s)
        if mm_m:
            return mm_m.group(1).strip()
        return ""

    return s


def strip_trailing_subtitle_punctuation(text: str) -> str:
    """
    Strips trailing punctuation marks ( ၊ , ။ ) from the end of subtitle lines.
    In professional movie subtitling, subtitle segments should never end with dangling
    commas (၊, ,) or periods/dandas (။).
    Handles single-line and multi-line subtitles (separated by \\n or \\N).
    """
    if not text:
        return ""
    s = str(text).strip()
    lines = s.split("\n")
    cleaned_lines = []
    for line in lines:
        parts = line.split("\\N")
        cleaned_parts = [re.sub(r'[\s၊,။]+$', '', p).strip() for p in parts]
        cleaned_lines.append("\\N".join(cleaned_parts))
    return "\n".join(cleaned_lines).strip()


def format_dual_speaker_subtitles(text: str) -> str:
    """
    Standardizes dual-speaker subtitle formatting with dialogue dashes (-).
    e.g. "- Are you ready? - Yes." -> "- Are you ready?\\N- Yes."
    Also strips trailing punctuation ( ၊ , ။ ) from each speaker's line.
    """
    if not text:
        return ""
    s = str(text).strip()
    if s.count("- ") >= 2 or (s.startswith("-") and ("\n-" in s or "\\N-" in s or " - " in s)):
        raw_parts = re.split(r'(?:\\N|\n|\s+-\s+)', s)
        cleaned = []
        for p in raw_parts:
            clean_p = p.strip().lstrip("-").strip()
            if clean_p:
                clean_p = strip_trailing_subtitle_punctuation(clean_p)
                cleaned.append(f"- {clean_p}")
        if len(cleaned) >= 2:
            return "\\N".join(cleaned)
    return strip_trailing_subtitle_punctuation(s)


def merge_short_gap_segments(
    segments: list,
    max_gap: float = 0.25,
    max_combined_dur: float = 5.0,
    max_combined_chars: int = 70
) -> list:
    """
    Consolidates fragmented micro-subtitles from speech recognition (Whisper).
    If two adjacent segments have a tiny pause (<= max_gap), neither has a strong sentence terminator,
    and their combined duration is <= max_combined_dur, merges them into a single coherent subtitle segment.
    """
    if not segments or len(segments) <= 1:
        return segments

    merged = []
    curr = dict(segments[0])

    for nxt in segments[1:]:
        curr_start = float(curr.get("start_s", curr.get("start", 0.0)))
        curr_end = float(curr.get("end_s", curr.get("end", 0.0)))
        nxt_start = float(nxt.get("start_s", nxt.get("start", 0.0)))
        nxt_end = float(nxt.get("end_s", nxt.get("end", 0.0)))

        gap = nxt_start - curr_end
        combined_dur = nxt_end - curr_start
        curr_text = str(curr.get("original", curr.get("text", ""))).strip()
        nxt_text = str(nxt.get("original", nxt.get("text", ""))).strip()
        combined_len = len(curr_text) + len(nxt_text) + 1

        ends_sentence = bool(re.search(r'[.!?။]$', curr_text))

        if (
            0.0 <= gap <= max_gap
            and combined_dur <= max_combined_dur
            and combined_len <= max_combined_chars
            and not ends_sentence
        ):
            if "end_s" in curr:
                curr["end_s"] = round(nxt_end, 3)
            if isinstance(curr.get("end"), str) or isinstance(nxt.get("end"), str):
                curr["end"] = nxt.get("end", "")
            else:
                curr["end"] = round(nxt_end, 3)
            if "end_ts" in curr and "end_ts" in nxt:
                curr["end_ts"] = nxt["end_ts"]
            curr["original"] = f"{curr_text} {nxt_text}"
            if "text" in curr:
                curr["text"] = curr["original"]
        else:
            merged.append(curr)
            curr = dict(nxt)

    merged.append(curr)
    for idx, s in enumerate(merged, 1):
        if "id" in s:
            s["id"] = idx
        if "no" in s:
            s["no"] = idx
    return merged


COMMON_MOVIE_IDIOMS_BURMESE = {
    r'\beat my dust\b': 'ငါ့နောက်ကသာ ပြေးလိုက်ခဲ့တော့',
    r'\bbreak a leg\b': 'ကံကောင်းပါစေ',
    r'\bdead meat\b': 'အသေပဲ',
    r'\bpiece of cake\b': 'လွယ်လွယ်လေးပါ',
    r'\bspill the beans\b': 'လျှို့ဝှက်ချက်ကို ဖွင့်ပြောလိုက်',
    r'\bbehind (?:my|your|his|her|their) back\b': 'ကွယ်ရာမှာ',
    r'\bbite the dust\b': 'အသက်ပျောက်သွားပြီ',
    r'\bover my dead body\b': 'ငါ့ကို အရင်သတ်သွားလိုက်',
    r'\bcut the crap\b': 'ပေါက်ကရတွေ တော်လိုက်တော့',
    r'\bshut up\b': 'ပါးစပ်ပိတ်ထား',
}


def localize_common_idioms(text: str) -> str:
    """Pre-localizes common movie idioms before LLM translation to avoid awkward literal translations."""
    if not text:
        return ""
    res = text
    for pattern, replacement in COMMON_MOVIE_IDIOMS_BURMESE.items():
        res = re.sub(pattern, replacement, res, flags=re.IGNORECASE)
    return res


# ─────────────────────────────────────────────────────────────────────────────
# Dual-Orthography Pipeline: TTS Phonetics vs Standard Subtitle Orthography
# (အသံထွက်ဖတ်သံ TTS စနစ် နှင့် စံမီမြန်မာစာလုံးပေါင်း သတ်ပုံအမှန် စာတန်းထိုးစနစ်)
# ─────────────────────────────────────────────────────────────────────────────

BURMESE_NUMBER_CLASSIFIERS = [
    'ယောက်', 'ခု', 'ခါ', 'နေ့', 'ချက်', 'ချိန်', 'ခေါက်', 'မိနစ်', 'စက္ကန့်', 'နာရီ',
    'တွဲ', 'သိုက်', 'ဝိုက်', 'ပိုင်း', 'ဝက်', 'နေရာ', 'ဖက်', 'ခြမ်း', 'ဦး', 'လျှောက်',
    'စု', 'ပြိုင်နက်', 'ကိုယ်လုံး', 'သက်လုံး', 'ထိုင်တည်း', 'လမ်းလုံး', 'ညလုံး',
    'ရက်', 'လ', 'နှစ်', 'ကြိမ်', 'သောင်း', 'ဆယ်', 'ရာ', 'ထောင်', 'သိန်း', 'မွှာ',
    'စုံ', 'စင်း', 'စီး', 'လုံး', 'ပါး', 'တန်', 'ကျပ်', 'ပြား', 'စ', 'စိပ်'
]

# Regex patterns for weak syllable number prefix 'တ-' <-> 'တစ်-'
PAT_CLASSIFIER_TO_STANDARD = re.compile(
    r'(?<![\u1039])တ(?![ေဲာ်ျြွှ\u102B-\u103E])(' + '|'.join(BURMESE_NUMBER_CLASSIFIERS) + r')'
)
PAT_CLASSIFIER_TO_PHONETIC = re.compile(
    r'(?<![\u1039])တစ်(' + '|'.join(BURMESE_NUMBER_CLASSIFIERS) + r')'
)

# Standard Myanmar Orthography Dictionary (မြန်မာစာလုံးပေါင်း သတ်ပုံကျမ်းနှင့်အညီ)
BURMESE_STANDARD_ORTHOGRAPHY_MAP = [
    ('ကျနော်တို့', 'ကျွန်တော်တို့'),
    ('ကျနော့်', 'ကျွန်တော့်'),
    ('ကျနော်', 'ကျွန်တော်'),
    ('ကျမတို့', 'ကျွန်မတို့'),
    ('ကျနုပ်', 'ကျွန်ုပ်'),
    ('အံ့သြ', 'အံ့ဩ'),
    ('အံ့အော', 'အံ့ဩ'),
    ('သြဇာ', 'ဩဇာ'),
    ('အောဇာ', 'ဩဇာ'),
    ('သြကာသ', 'ဩကာသ'),
    ('အောကာသ', 'ဩကာသ'),
    ('သြဝါဒ', 'ဩဝါဒ'),
    ('အောဝါဒ', 'ဩဝါဒ'),
    ('ဥက္ကဌ', 'ဥက္ကဋ္ဌ'),
    ('အုတ်ကထ', 'ဥက္ကဋ္ဌ'),
    ('ယောကျာ်း', 'ယောကျ်ား'),
    ('ယောင်္ကျား', 'ယောကျ်ား'),
    ('ယောက်ျား', 'ယောကျ်ား'),
    ('ဝတ်ထု', 'ဝတ္ထု'),
    ('သမ်မတ', 'သမ္မတ'),
    ('မိတ်တာ', 'မေတ္တာ'),
    ('ဒုတ်ခ', 'ဒုက္ခ'),
    ('အန်တရာယ်', 'အန္တရာယ်'),
    ('ပင်ညာ', 'ပညာ'),
    ('ဝိန်ညာဉ်', 'ဝိညာဉ်'),
    ('သိတ်ပံ', 'သိပ္ပံ'),
    ('ဗုတ်ဒ', 'ဗုဒ္ဓ'),
    ('မင်ဂလာ', 'မင်္ဂလာ'),
    ('သိတ်စာ', 'သစ္စာ'),
    ('ကိတ်စ', 'ကိစ္စ'),
    ('မစ်စတာ', 'မစ္စတာ'),
    ('အဖွား', 'အဘွား'),
    ('အဖိုး', 'အဘိုး'),
    ('ပီးတော့', 'ပြီးတော့'),
    ('ပီးရင်', 'ပြီးရင်'),
    ('ပီးပီ', 'ပြီးပြီ'),
    ('ကောင်းပီ', 'ကောင်းပြီ'),
]

# Spoken Phonetics Dictionary for Microsoft Edge Neural TTS (my-MM-ThihaNeural / my-MM-NilarNeural)
BURMESE_TTS_PHONETIC_MAP = [
    ('ကျွန်တော်တို့', 'ကျနော်တို့'),
    ('ကျွန်တော့်', 'ကျနော့်'),
    ('ကျွန်တော်', 'ကျနော်'),
    ('ကျွန်မတို့', 'ကျမတို့'),
    ('ကျွန်မ', 'ကျမ'),
    ('ကျွန်ုပ်', 'ကျနုပ်'),
    ('ဥက္ကဋ္ဌ', 'အုတ်ကထ'),
    ('ဥက္ကဌ', 'အုတ်ကထ'),
    ('ဝတ္ထု', 'ဝတ်ထု'),
    ('သမ္မတ', 'သမ်မတ'),
    ('မေတ္တာ', 'မိတ်တာ'),
    ('ဒုက္ခ', 'ဒုတ်ခ'),
    ('အန္တရာယ်', 'အန်တရာယ်'),
    ('ပညာ', 'ပင်ညာ'),
    ('ဝိညာဉ်', 'ဝိန်ညာဉ်'),
    ('သိပ္ပံ', 'သိတ်ပံ'),
    ('ဗုဒ္ဓ', 'ဗုတ်ဒ'),
    ('မင်္ဂလာ', 'မင်ဂလာ'),
    ('သစ္စာ', 'သိတ်စာ'),
    ('ကိစ္စ', 'ကိတ်စ'),
    ('မစ္စတာ', 'မစ်စတာ'),
    ('အံ့ဩ', 'အံ့အော'),
    ('အံ့သြ', 'အံ့အော'),
    ('ဩဇာ', 'အောဇာ'),
    ('သြဇာ', 'အောဇာ'),
    ('ဩကာသ', 'အောကာသ'),
    ('သြကာသ', 'အောကာသ'),
    ('ဩဝါဒ', 'အောဝါဒ'),
    ('သြဝါဒ', 'အောဝါဒ'),
    ('ယောကျ်ား', 'ယောက်ျား'),
    ('ယောကျာ်း', 'ယောက်ျား'),
    ('ယောင်္ကျား', 'ယောက်ျား'),
    ('ပြီးတော့', 'ပီးတော့'),
    ('ပြီးရင်', 'ပီးရင်'),
    ('ပြီးပြီ', 'ပီးပီ'),
    ('ကောင်းပြီ', 'ကောင်းပီ'),
    ('အကယ်၍', 'အကယ်ရွေ့'),
]


def convert_to_tts_phonetic_burmese(text: str) -> str:
    """
    Converts written/formal Myanmar text into natural phonetic spoken orthography (အသံထွက်ဖတ်သံ)
    for Microsoft Edge Neural Voices (my-MM-ThihaNeural, my-MM-NilarNeural) and F5-TTS:
      1. Pronouns: ကျွန်တော် -> ကျနော်, ကျွန်မ -> ကျမ, etc.
      2. Connected classifiers: တစ်ယောက် -> တယောက်, တစ်ခု -> တခု, တစ်ခါ -> တခါ, etc.
      3. Stacked Pali ligatures: ဥက္ကဋ္ဌ -> အုတ်ကထ, ဝတ္ထု -> ဝတ်ထု, သမ္မတ -> သမ်မတ, etc.
      4. Smooth spoken conjunctions: အကယ်၍ -> အကယ်ရွေ့, ပြီးတော့ -> ပီးတော့.
    This eliminates robotic halts, glottal stuttering, and unpronounced stacked consonants.
    """
    if not text:
        return ""
    s = str(text)

    # 1. Lexical phonetic mappings
    for formal, spoken in BURMESE_TTS_PHONETIC_MAP:
        s = s.replace(formal, spoken)

    # 2. Number before classifier: တစ် -> တ
    s = PAT_CLASSIFIER_TO_PHONETIC.sub(r'တ\1', s)

    return s


def normalize_standard_burmese_spelling(text: str) -> str:
    """
    Normalizes colloquial spoken forms and phonetic text back into 100% correct
    standard Myanmar orthography (မြန်မာစာလုံးပေါင်း သတ်ပုံကျမ်းနှင့်အညီ သတ်ပုံအမှန်)
    for professional movie & drama subtitles (.srt, .ass, hardsub):
      1. Pronouns: ကျနော် -> ကျွန်တော်, ကျမ -> ကျွန်မ, etc.
      2. Spoken numbers: တယောက် -> တစ်ယောက်, တခု -> တစ်ခု, တခါ -> တစ်ခါ, etc.
      3. Standard orthography: အံ့သြ/အံ့အော -> အံ့ဩ, ဥက္ကဌ/အုတ်ကထ -> ဥက္ကဋ္ဌ, ယောကျာ်း/ယောက်ျား -> ယောကျ်ား.
    """
    if not text:
        return ""
    s = str(text)

    # 1. Lexical and orthographic corrections
    for colloquial, standard in BURMESE_STANDARD_ORTHOGRAPHY_MAP:
        s = s.replace(colloquial, standard)

    # 2. Feminine pronoun ကျမ -> ကျွန်မ with context boundary check
    s = re.sub(r'(^|[\s၊။!?])ကျမ(?=[\s၊။!?]|က|ကို|ရဲ့|တို့|မှာ|လည်း|အတွက်|ဆီ|ဖြင့်|ဖြင့်|[က-အ])', r'\1ကျွန်မ', s)

    # 3. Spoken number before classifier: တ -> တစ်
    s = PAT_CLASSIFIER_TO_STANDARD.sub(r'တစ်\1', s)

    return s





