from brain.diagnostics import log_suppressed
import json
import os
import re
from brain.memory import MovieState
from brain.prompts import SEO_SYSTEM_PROMPT, get_seo_prompt
from brain import config as cfg
from brain.gemini_client import call_gemini

class SEOAgent:
    def __init__(self, language: str = "burmese"):
        self.language = language

    def _call_gemini(self, system_prompt: str, user_prompt: str, api_key: str, model: str) -> str:
        text, used_model = call_gemini(system_prompt, user_prompt, api_key, model, temperature=0.5)
        print(f"[*] SEOAgent: Google API model '{used_model}' responded successfully.")
        return text

    def generate_seo(self, state: MovieState) -> MovieState:
        """Generates high-CTR YouTube Title, Description, Keywords, and Hashtags via Gemini API."""
        print(f"[*] SEOAgent: Generating viral YouTube SEO metadata (Lang: {self.language})...")

        user_prompt = get_seo_prompt(
            state.movie_name,
            state.genre or "Action / Drama",
            state.story_structure or {},
            language=self.language
        )

        # Check if Gemini API is enabled in config
        config_data = cfg.load_config()
        gemini_cfg = config_data.get("gemini", {})
        gemini_enabled = gemini_cfg.get("enabled", False)
        gemini_key = gemini_cfg.get("api_keys") or os.getenv("GEMINI_API_KEYS") or os.getenv("GEMINI_API_KEY") or ""
        models_dict = gemini_cfg.get("models", {})
        gemini_model = models_dict.get("workhorse", "gemini-3.5-flash-lite")

        if gemini_enabled and gemini_key:
            try:
                print(f"[*] SEOAgent: Calling Google Gemini API ({gemini_model}) for viral YouTube title & tags...")
                raw_content = self._call_gemini(SEO_SYSTEM_PROMPT, user_prompt, gemini_key, gemini_model)
                seo_data = self._parse_json(raw_content)
                if seo_data:
                    state.seo_metadata = seo_data
                    print("[OK] SEOAgent: Successfully generated viral YouTube Title & Tags via Gemini API!")
                    self.export_final_outputs(state)
                    return state
                else:
                    print("[!] SEOAgent Warning: Could not parse Gemini response as JSON. Falling back to heuristic SEO...")
            except Exception as e:
                print(f"[!] SEOAgent Warning: Gemini API call failed ({type(e).__name__}: {e}). Falling back to heuristic SEO...")

        state.seo_metadata = self._heuristic_seo(state)
        self.export_final_outputs(state)
        return state

    def _parse_json(self, raw_content: str):
        clean_json = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw_content, flags=re.MULTILINE).strip()
        parsed = None
        for candidate in [clean_json, raw_content]:
            # Strip trailing commas before closing braces/brackets which LLMs often add
            cleaned = re.sub(r',\s*([\]}])', r'\1', candidate)
            try:
                parsed = json.loads(cleaned)
                break
            except json.JSONDecodeError as _exc:
                log_suppressed("agents.seo_agent._parse_json", _exc)
            start = cleaned.find('{')
            end = cleaned.rfind('}')
            if start != -1 and end != -1 and end > start:
                try:
                    parsed = json.loads(cleaned[start:end+1])
                    break
                except json.JSONDecodeError as _exc:
                    log_suppressed("agents.seo_agent._parse_json", _exc)
            m = re.search(r'\{.*\}', cleaned, re.DOTALL)
            if m:
                try:
                    parsed = json.loads(m.group())
                    break
                except json.JSONDecodeError as _exc:
                    log_suppressed("agents.seo_agent._parse_json", _exc)

        if isinstance(parsed, dict):
            # Normalize keywords to clean list
            kw = parsed.get("keywords")
            if isinstance(kw, str):
                parsed["keywords"] = [k.strip() for k in kw.split(",") if k.strip()]
            elif not isinstance(kw, list):
                parsed["keywords"] = []

            # Normalize hashtags to clean list
            ht = parsed.get("hashtags")
            if isinstance(ht, str):
                parsed["hashtags"] = [h.strip() for h in ht.split() if h.strip()]
            elif not isinstance(ht, list):
                parsed["hashtags"] = []

            return parsed
        return None

    def _heuristic_seo(self, state: MovieState) -> dict:
        """Generate high-ranking SEO metadata without LLM."""
        clean_title = (state.movie_name or "Movie").replace("_", " ").title()
        chars = ", ".join(state.characters[:2]) if state.characters else "the characters"
        story = state.story_structure or {}
        genre = state.genre or "Action/Drama"
        twist = story.get("twist", "an unexpected turning point that changes everything")
        print(f"[OK] SEOAgent: Heuristic SEO metadata generated successfully (Lang: {getattr(self, 'language', 'burmese')}).")
        
        is_burmese = getattr(self, "language", "burmese").lower() in ["burmese", "mm", "myanmar"]
        if is_burmese:
            return {
                "title": f"သူ့ကို ဘာလို့ ဒီလောက် ရက်စက်စွာ အကျဉ်းချခဲ့တာလဲ... | {clean_title} Movie Recap Myanmar",
                "description": (
                    f"ကြည့်နေရင်းနဲ့တောင် ကြက်သီးထစရာ ကောင်းလောက်အောင် ထိတ်လန့်စရာ ဇာတ်အိမ်နဲ့ '{clean_title}' ({genre}) ဇာတ်ကားကြီးရဲ့ ဇာတ်လမ်းအကျဉ်းကို အသေးစိတ် ရှင်းပြပေးလိုက်ပါတယ်။\n\n"
                    f"အဓိက ဇာတ်လိုက် {chars} တို့ရဲ့ ရင်ခုန်စရာ အချိုးအကွေ့တွေနဲ့ {twist} ကို တစ်ကွက်မကျန် စိတ်လှုပ်ရှားစွာ ကြည့်ရှုခံစားနိုင်မှာပါ။\n\n"
                    f"ဒီလို ဇာတ်လမ်းကောင်းတွေကို နေ့စဉ် ကြည့်ရှုနိုင်ဖို့ Like, Share နဲ့ Subscribe လုပ်ထားပေးကြပါဦး ခင်ဗျာ!"
                ),
                "keywords": [
                    clean_title, "myanmar movie recap", "movie recap myanmar", "ဇာတ်လမ်းအကျဉ်း",
                    "myanmar sub", "film explained myanmar", "story recap",
                    "myanmar review", "ending explained", "viral recap",
                    genre.lower(), "full movie recap"
                ],
                "hashtags": ["#myanmarmovierecap", "#ဇာတ်လမ်းအကျဉ်း", "#movierecap", "#filmexplained", "#myanmarsub"]
            }
        else:
            return {
                "title": f"Why Everyone Is Talking About This | {clean_title} Full Recap Explained",
                "description": (
                    f"In this video, we break down the gripping story of '{clean_title}' "
                    f"— a {genre} that follows {chars} through danger, betrayal, and {twist}.\n\n"
                    f"We analyze every major scene, character motivation, and plot twist so you never miss a detail. "
                    f"Whether you watched it or not, this recap will leave you speechless.\n\n"
                    f"Like and subscribe for daily movie recaps, film explanations, and story breakdowns!"
                ),
                "keywords": [
                    clean_title, "movie recap", "film explained", "story recap",
                    "mystery recapped", "movie review", "ending explained",
                    "best movies 2024", "recap channel", "viral recap",
                    genre.lower(), "plot twist explained", "full movie recap"
                ],
                "hashtags": ["#movierecap", "#filmexplained", "#storyrecap", "#endingexplained", "#cinema"]
            }

    def export_final_outputs(self, state: MovieState, output_dir: str = None):
        """
        Exports readable TXT script and JSON metadata into the output directory
        (``outputs/<project>`` unless ``output_dir`` names the project folder explicitly).
        """
        if output_dir is None:
            import brain.config as cfg
            config_data = cfg.load_config()
            output_base = config_data.get("paths", {}).get("output_dir", "outputs")
            output_dir = os.path.join(output_base, state.project_dir)
        os.makedirs(output_dir, exist_ok=True)

        # 1. Export human-readable script TXT
        txt_path = os.path.join(output_dir, "final_recap_script.txt")
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write("==================================================\n")
            f.write(f"[VIDEO] MOVIE RECAP SCRIPT: {(state.movie_name or 'UNKNOWN').upper()}\n")
            f.write("==================================================\n\n")
            
            if state.seo_metadata:
                tags = state.seo_metadata.get('hashtags', [])
                tag_str = ' '.join(tags) if isinstance(tags, list) else str(tags or '')
                keys = state.seo_metadata.get('keywords', [])
                key_str = ', '.join(keys) if isinstance(keys, list) else str(keys or '')
                f.write(f"[PIN] YOUTUBE TITLE: {state.seo_metadata.get('title', 'N/A')}\n")
                f.write(f"[TAG] HASHTAGS: {tag_str}\n\n")
                f.write(f"[TEXT] DESCRIPTION:\n{state.seo_metadata.get('description', '')}\n\n")
                f.write(f"[KEY] KEYWORDS:\n{key_str}\n")
                f.write("--------------------------------------------------\n\n")

            f.write("[NARRATION] VOICE-OVER NARRATION SCRIPT\n")
            f.write("--------------------------------------------------\n\n")
            if state.generated_script:
                for idx, item in enumerate(state.generated_script, start=1):
                    f.write(f"--- [Scene {item.get('scene_id', idx)}] ---\n")
                    f.write(f"[NARRATION] NARRATION : {item.get('narration', '')}\n")
                    f.write(f"[CUE] VISUAL CUE: {item.get('visual_cue', '')}\n\n")
            else:
                f.write("No script generated.\n")

        # 2. Export standalone SEO metadata JSON
        seo_path = os.path.join(output_dir, "seo_metadata.json")
        with open(seo_path, "w", encoding="utf-8") as f:
            json.dump(state.seo_metadata or {}, f, ensure_ascii=False, indent=4)

        print(f"[OUTPUT] SEOAgent: Final human-readable script exported to -> {txt_path}")
        print(f"[OUTPUT] SEOAgent: SEO Metadata exported to -> {seo_path}")
