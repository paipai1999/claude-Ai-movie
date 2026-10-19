"""
agents.translation_flow - context windows, validator-driven retries, batch splitting, per-line
fallback, review flags and the outage circuit breaker. A scripted fake stands in for Gemini.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents import translation_flow as tf  # noqa: E402
from brain.gemini_client import GeminiContentError  # noqa: E402

MM = "ဒီဇာတ်လမ်းမှာတော့ ဇာတ်ကောင်ကြီးက မြို့ထဲကို ပြန်လာခဲ့တာပေါ့ဗျာ"


def mm(n: int) -> str:
    """A distinct, valid Burmese line (distinct enough to never look like a neighbour duplicate)."""
    words = ["မနက်ဖြန်", "ညနေခင်း", "တံခါးဝ", "ရထားဘူတာ", "ဆေးရုံကြီး", "ကျောင်းနား", "စျေးထဲ", "တောင်ကြော"]
    verbs = ["ပြေးသွားခဲ့တာပေါ့ဗျာ", "စောင့်နေခဲ့ပါတယ်", "ရောက်လာခဲ့ရတယ်", "ပျောက်သွားခဲ့တာပါ"]
    return f"{words[n % len(words)]}မှာ {words[(n * 3 + 1) % len(words)]}ကို {verbs[n % len(verbs)]} {n * 7 + 11}"


def segs(n: int):
    return [{"id": i, "text": f"Source line number {i} spoken in the film.", "start_sec": float(i), "end_sec": i + 1.0} for i in range(1, n + 1)]


class FakeModel:
    """Callable pair for TranslationFlow with a scripted behaviour per call."""

    def __init__(self, batch_script=None, line_script=None):
        self.batch_calls = []   # (ids, context, feedback)
        self.line_calls = []
        self._batch_script = batch_script or (lambda ids, ctx, fb, n: [{"id": i, "narration": mm(i)} for i in ids])
        self._line_script = line_script or (lambda seg, ctx: mm(seg["id"]))

    def batch(self, lines, context, feedback):
        ids = [s["id"] for s in lines]
        self.batch_calls.append((ids, context, feedback))
        return self._batch_script(ids, context, feedback, len(self.batch_calls))

    def line(self, seg, context):
        self.line_calls.append((seg["id"], context))
        return self._line_script(seg, context)


def make_flow(model, **kw):
    logs = []
    flow = tf.TranslationFlow(model.batch, model.line, log=logs.append, **kw)
    return flow, logs


class HappyPathTests(unittest.TestCase):
    def test_every_line_translated_in_order_with_no_flags(self):
        model = FakeModel()
        flow, _ = make_flow(model)
        out = flow.run(segs(45))
        self.assertEqual([r.seg_id for r in out], list(range(1, 46)))
        self.assertTrue(all(r.narration == mm(r.seg_id) and not r.needs_review and r.origin == "batch" for r in out))
        self.assertEqual(len(model.batch_calls), 3)        # 20 + 20 + 5
        self.assertEqual(model.line_calls, [])

    def test_context_carries_previous_translations_and_upcoming_source(self):
        model = FakeModel()
        flow, _ = make_flow(model)
        flow.run(segs(45))
        first_ctx, second_ctx = model.batch_calls[0][1], model.batch_calls[1][1]
        self.assertEqual(first_ctx["previous"], [])
        self.assertEqual([x["id"] for x in first_ctx["next"]], [21, 22, 23])
        self.assertEqual(first_ctx["next"][0]["text"], "Source line number 21 spoken in the film.")
        self.assertEqual([x["id"] for x in second_ctx["previous"]], [18, 19, 20])
        self.assertEqual(second_ctx["previous"][-1]["narration"], mm(20))
        self.assertEqual(model.batch_calls[2][1]["next"], [])   # nothing follows the last batch

    def test_item_fields_are_kept_and_normalised(self):
        def script(ids, ctx, fb, n):
            return [{"id": str(i), "narration": mm(i), "emotion": "angry", "character": "Mia", "gender": "FEMALE"} for i in ids]
        flow, _ = make_flow(FakeModel(batch_script=script))
        r = flow.run(segs(3))[0]
        self.assertEqual((r.emotion, r.character, r.gender), ("angry", "Mia", "female"))

    def test_alternate_text_keys_and_child_gender(self):
        def script(ids, ctx, fb, n):
            return [{"id": i, "translation": mm(i) + " သမီး", "gender": "child"} for i in ids]
        flow, _ = make_flow(FakeModel(batch_script=script))
        r = flow.run(segs(2))[0]
        self.assertEqual(r.gender, "female")
        self.assertIn("သမီး", r.narration)

    def test_cleaners_run_in_the_right_order(self):
        def script(ids, ctx, fb, n):
            return [{"id": i, "narration": mm(i)} for i in ids]
        flow, _ = make_flow(FakeModel(batch_script=script), pre_clean=lambda t: t + " (pre)", final_clean=lambda t: t + " (final)")
        r = flow.run(segs(1))[0]
        self.assertTrue(r.narration.endswith("(pre) (final)"))


class CorrectiveRetryTests(unittest.TestCase):
    def test_only_bad_lines_are_re_asked_with_specific_feedback(self):
        def script(ids, ctx, fb, n):
            if n == 1:   # line 3 comes back English, line 5 is missing
                items = [{"id": i, "narration": mm(i)} for i in ids if i != 5]
                for it in items:
                    if it["id"] == 3:
                        it["narration"] = "I will find you wherever you are hiding"
                return items
            return [{"id": i, "narration": mm(i)} for i in ids]
        model = FakeModel(batch_script=script)
        flow, _ = make_flow(model)
        out = flow.run(segs(8))
        self.assertEqual(len(model.batch_calls), 2)
        retry_ids, _ctx, feedback = model.batch_calls[1]
        self.assertEqual(retry_ids, [3, 5])
        self.assertIn("- id 3:", feedback)
        self.assertIn("Burmese", feedback)
        self.assertIn("- id 5:", feedback)
        self.assertTrue(all(not r.needs_review for r in out))
        self.assertEqual(out[2].origin, "retry")
        self.assertEqual(out[0].origin, "batch")
        self.assertEqual(flow.stats["retries"], 1)

    def test_a_worse_retry_does_not_replace_a_better_first_answer(self):
        good_but_dup = mm(2)

        def script(ids, ctx, fb, n):
            if n == 1:
                return [{"id": i, "narration": (mm(i) if i != 2 else "I will find you again soon")} for i in ids]
            return [{"id": i, "narration": ""} for i in ids]   # retries return empties
        flow, _ = make_flow(FakeModel(batch_script=script, line_script=lambda s, c: good_but_dup))
        out = flow.run(segs(4))
        self.assertEqual(out[0].narration, mm(1))              # untouched by the retries
        self.assertEqual(out[1].narration, good_but_dup)       # fixed by the per-line fallback
        self.assertEqual(out[1].origin, "line")

    def test_neighbour_duplicate_is_flagged_but_never_blocks(self):
        def script(ids, ctx, fb, n):
            return [{"id": i, "narration": mm(1)} for i in ids]    # every line identical
        model = FakeModel(batch_script=script)
        flow, _ = make_flow(model)
        out = flow.run(segs(3))
        self.assertEqual(len(model.batch_calls), 1)               # no retry for a style issue
        self.assertEqual(model.line_calls, [])
        self.assertEqual(out[0].issues, [])
        self.assertEqual(out[1].issues, ["duplicate"])
        self.assertTrue(out[1].needs_review)
        self.assertEqual(flow.stats["flagged"], 2)


class SplitTests(unittest.TestCase):
    def test_unparsable_answer_splits_the_batch(self):
        def script(ids, ctx, fb, n):
            return None if len(ids) > 10 else [{"id": i, "narration": mm(i)} for i in ids]
        model = FakeModel(batch_script=script)
        flow, logs = make_flow(model)
        out = flow.run(segs(20))
        self.assertEqual([len(c[0]) for c in model.batch_calls], [20, 10, 10])
        self.assertTrue(all(r.narration == mm(r.seg_id) and not r.needs_review for r in out))
        self.assertEqual(flow.stats["splits"], 1)
        self.assertTrue(any("Splitting" in m for m in logs))

    def test_split_halves_see_each_others_context(self):
        def script(ids, ctx, fb, n):
            return None if len(ids) > 10 else [{"id": i, "narration": mm(i)} for i in ids]
        model = FakeModel(batch_script=script)
        flow, _ = make_flow(model)
        flow.run(segs(20))
        left_ctx, right_ctx = model.batch_calls[1][1], model.batch_calls[2][1]
        self.assertEqual([x["id"] for x in left_ctx["next"]], [11, 12, 13])
        self.assertEqual([x["id"] for x in right_ctx["previous"]], [8, 9, 10])
        self.assertEqual(right_ctx["previous"][-1]["narration"], mm(10))

    def test_blocked_content_is_isolated_down_to_the_offending_line(self):
        def script(ids, ctx, fb, n):
            if 7 in ids and len(ids) > 1:
                raise GeminiContentError("Gemini API blocked response: PROHIBITED_CONTENT")
            return [{"id": i, "narration": mm(i)} for i in ids]
        def line_script(seg, ctx):
            if seg["id"] == 7:
                raise GeminiContentError("blocked")
            return mm(seg["id"])
        model = FakeModel(batch_script=script, line_script=line_script)
        flow, _ = make_flow(model)
        out = flow.run(segs(12))
        flagged = [r.seg_id for r in out if r.needs_review]
        self.assertEqual(flagged, [7])                       # only the offending line is lost
        self.assertEqual(out[6].narration, "Source line number 7 spoken in the film.")   # source kept, flagged
        self.assertEqual(out[6].origin, "source")
        self.assertIn("missing", out[6].issues)
        self.assertTrue(all(r.narration == mm(r.seg_id) for r in out if r.seg_id != 7))
        self.assertEqual({r.seg_id: r.origin for r in out}[8], "line")   # its neighbours were rescued one by one
        self.assertEqual(flow.stats["splits"], 2)

    def test_small_batches_are_not_split(self):
        model = FakeModel(batch_script=lambda ids, ctx, fb, n: None)
        flow, _ = make_flow(model)
        out = flow.run(segs(4))
        self.assertEqual(flow.stats["splits"], 0)
        self.assertEqual(len(model.line_calls), 4)               # straight to per-line
        self.assertTrue(all(r.narration == mm(r.seg_id) for r in out))   # line fallback produced them
        self.assertTrue(all(r.origin == "line" for r in out))


class LineFallbackTests(unittest.TestCase):
    def test_unresolvable_line_keeps_source_and_is_flagged(self):
        def script(ids, ctx, fb, n):
            return [{"id": i, "narration": ("Still English text here" if i == 2 else mm(i))} for i in ids]
        model = FakeModel(batch_script=script, line_script=lambda s, c: "Another English attempt")
        flow, _ = make_flow(model)
        out = flow.run(segs(3))
        self.assertTrue(out[1].needs_review)
        self.assertIn("untranslated", out[1].issues)
        self.assertEqual(out[1].narration, "Still English text here")       # best available, not lost
        self.assertEqual([r.needs_review for r in out], [False, True, False])

    def test_line_fallback_gets_the_previous_translation_as_context(self):
        def script(ids, ctx, fb, n):
            return [{"id": i, "narration": ("" if i == 3 else mm(i))} for i in ids]
        model = FakeModel(batch_script=script)
        flow, _ = make_flow(model)
        flow.run(segs(5))
        seg_id, ctx = model.line_calls[0]
        self.assertEqual(seg_id, 3)
        self.assertEqual(ctx["previous"][-1]["narration"], mm(2))


class OutageTests(unittest.TestCase):
    def test_three_service_failures_in_a_row_stop_the_run_with_partial_results(self):
        def script(ids, ctx, fb, n):
            if n <= 1:
                return [{"id": i, "narration": mm(i)} for i in ids]
            raise TimeoutError("network down")
        model = FakeModel(batch_script=script)
        flow, logs = make_flow(model)
        with self.assertRaises(tf.TranslationUnavailable) as caught:
            flow.run(segs(100))
        self.assertEqual(len(caught.exception.partial), 20 + 20 + 20)      # first ok, then two failed windows kept as flagged
        self.assertTrue(all(r.needs_review for r in caught.exception.partial[20:]))
        self.assertEqual(len(model.batch_calls), 4)                         # 1 ok + 3 failures, then it stops
        self.assertEqual(model.line_calls, [])                              # no per-line spam during an outage
        self.assertIn("Gemini", str(caught.exception))
        self.assertIn("မ", str(caught.exception))                           # bilingual message

    def test_a_single_failure_is_survivable(self):
        def script(ids, ctx, fb, n):
            if n == 2:
                raise TimeoutError("blip")
            return [{"id": i, "narration": mm(i)} for i in ids]
        flow, _ = make_flow(FakeModel(batch_script=script))
        out = flow.run(segs(60))
        self.assertEqual(len(out), 60)
        self.assertEqual([r.seg_id for r in out if r.needs_review], list(range(21, 41)))
        self.assertTrue(all(r.origin == "source" for r in out[20:40]))

    def test_user_cancel_is_never_swallowed(self):
        def script(ids, ctx, fb, n):
            raise InterruptedError("stopped")
        flow, _ = make_flow(FakeModel(batch_script=script))
        with self.assertRaises(InterruptedError):
            flow.run(segs(5))

    def test_cancel_hook_is_called_between_windows(self):
        calls = []
        flow, _ = make_flow(FakeModel(), check_cancel=lambda: calls.append(1))
        flow.run(segs(41))
        self.assertGreaterEqual(len(calls), 3)


class LargeScaleTests(unittest.TestCase):
    def test_hundreds_of_lines_with_random_faults_all_come_back(self):
        import random
        rng = random.Random(7)

        def script(ids, ctx, fb, n):
            items = []
            for i in ids:
                roll = rng.random()
                if roll < 0.08:
                    continue                                  # dropped
                items.append({"id": i, "narration": mm(i) if roll > 0.15 else "English leftovers appear here"})
            return items
        model = FakeModel(batch_script=script)
        flow, _ = make_flow(model)
        out = flow.run(segs(300))
        self.assertEqual([r.seg_id for r in out], list(range(1, 301)))
        self.assertTrue(all(r.narration for r in out))
        bad = [r for r in out if r.needs_review]
        self.assertLess(len(bad), 3)           # retries + per-line fallback fix almost everything


if __name__ == "__main__":
    unittest.main()
