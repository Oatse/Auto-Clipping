"""Tests for processors.translator helpers."""

from __future__ import annotations

from models.transcript import WordTimestamp
from processors.translator.gemini_client import repair_truncated_json
from processors.translator.local_grouper import (
    local_group_from_segments,
    local_group_words,
)
from processors.translator.regrouper import (
    build_word_batches,
    reconstruct_segments,
)


def _w(word, start, end):
    return WordTimestamp(word=word, start=start, end=end)


# ─── repair_truncated_json ───────────────────────────────────────────────

class TestRepairTruncatedJson:
    def test_complete_groups_extracted_from_truncated_response(self):
        raw = '''
        [
          {"indices": [0, 1, 2], "translated": "Hello world"},
          {"indices": [3, 4], "translated": "Goodbye"},
          {"indices": [5, 6], "transl
        '''
        groups = repair_truncated_json(raw)
        assert groups is not None
        assert len(groups) == 2
        assert groups[0]["translated"] == "Hello world"
        assert groups[1]["translated"] == "Goodbye"

    def test_empty_input_returns_none(self):
        assert repair_truncated_json("") is None
        assert repair_truncated_json("garbage") is None

    def test_escaped_quotes_unescaped(self):
        raw = '[{"indices": [0], "translated": "She said \\"hi\\""}]'
        groups = repair_truncated_json(raw)
        assert groups is not None
        assert groups[0]["translated"] == 'She said "hi"'


# ─── build_word_batches ─────────────────────────────────────────────────

class TestBuildWordBatches:
    def test_short_input_returns_single_batch(self):
        words = [_w(str(i), float(i), float(i) + 0.5) for i in range(10)]
        speakers = ["SPEAKER_00"] * 10
        batches = build_word_batches(words, speakers, max_words=20)
        assert len(batches) == 1
        assert batches[0][0] == words

    def test_long_input_split_at_pause(self):
        # Insert a long pause in the middle to force a break
        words = []
        speakers = []
        for i in range(40):
            t = float(i) * 0.5  # 0.5 s per word, no pause
            words.append(_w(str(i), t, t + 0.4))
            speakers.append("SPEAKER_00")
        # Inject 2-second silence after word 20
        for i in range(20, 40):
            words[i].start += 2.0
            words[i].end += 2.0

        batches = build_word_batches(words, speakers, max_words=25)
        assert len(batches) >= 2

    def test_split_at_speaker_change(self):
        words = [_w(str(i), float(i), float(i) + 0.5) for i in range(40)]
        speakers = ["SPEAKER_00"] * 20 + ["SPEAKER_01"] * 20
        batches = build_word_batches(words, speakers, max_words=25)
        # Should split at the speaker change near index 20
        assert len(batches) >= 2


# ─── reconstruct_segments ───────────────────────────────────────────────

class TestReconstructSegments:
    def test_simple_grouping(self):
        words = [_w("hello", 0.0, 0.5), _w("world", 0.5, 1.0)]
        speakers = ["SPEAKER_00", "SPEAKER_00"]
        groups = [{"indices": [0, 1], "translated": "Halo dunia"}]
        segs = reconstruct_segments(groups, words, speakers)
        assert len(segs) == 1
        assert segs[0].text == "Halo dunia"
        assert segs[0].start == 0.0
        assert segs[0].end == 1.0

    def test_punctuation_only_group_skipped_and_absorbed(self):
        words = [
            _w("hi", 0.0, 0.5),
            _w(".", 0.5, 0.6),
        ]
        speakers = ["SPEAKER_00", "SPEAKER_00"]
        groups = [
            {"indices": [0], "translated": "Halo"},
            {"indices": [1], "translated": "."},  # punctuation-only — skipped
        ]
        segs = reconstruct_segments(groups, words, speakers)
        # Only one real segment kept
        assert len(segs) == 1
        # The "." word was absorbed into the previous segment
        assert len(segs[0].words) == 2
        assert segs[0].end == 0.6

    def test_missing_word_absorbed_into_nearest(self):
        words = [_w(w, i * 0.5, i * 0.5 + 0.4) for i, w in enumerate(["a", "b", "c"])]
        speakers = ["SPEAKER_00"] * 3
        # Gemini only returns indices 0 and 2 — index 1 is missing
        groups = [
            {"indices": [0], "translated": "A"},
            {"indices": [2], "translated": "C"},
        ]
        segs = reconstruct_segments(groups, words, speakers)
        # Both segments exist; word 'b' absorbed into nearest one
        all_words = [w.word for s in segs for w in s.words]
        assert "b" in all_words

    def test_invalid_indices_dropped(self):
        words = [_w("hi", 0.0, 0.5)]
        speakers = ["SPEAKER_00"]
        groups = [
            {"indices": [99], "translated": "out of range"},
            {"indices": [0], "translated": "Hi"},
        ]
        segs = reconstruct_segments(groups, words, speakers)
        # Only the valid group becomes a segment
        assert len(segs) == 1
        assert segs[0].text == "Hi"


# ─── local_group_words ──────────────────────────────────────────────────

class TestLocalGrouper:
    def test_pause_triggers_split(self):
        words = [
            _w("hello", 0.0, 0.5),
            _w("world", 0.5, 1.0),
            _w("bye", 3.0, 3.5),  # >0.7 s pause
        ]
        speakers = ["SPEAKER_00"] * 3
        segs = local_group_words(words, speakers)
        assert len(segs) == 2

    def test_speaker_change_triggers_split(self):
        words = [
            _w("hi", 0.0, 0.5),
            _w("hello", 0.5, 1.0),
        ]
        speakers = ["SPEAKER_00", "SPEAKER_01"]
        segs = local_group_words(words, speakers)
        assert len(segs) == 2

    def test_word_count_cap(self):
        words = [_w(f"w{i}", float(i) * 0.1, float(i) * 0.1 + 0.05) for i in range(30)]
        speakers = ["SPEAKER_00"] * 30
        segs = local_group_words(words, speakers)
        # 30 words, max 12 per segment → at least 3 segments
        assert len(segs) >= 3
        for seg in segs:
            assert len(seg.words) <= 12

    def test_sentence_boundary_triggers_split(self):
        words = [
            _w("Hello", 0.0, 0.5),
            _w("there", 0.5, 1.0),
            _w("world.", 1.0, 1.5),
            _w("Bye", 1.5, 2.0),
        ]
        speakers = ["SPEAKER_00"] * 4
        segs = local_group_words(words, speakers)
        # ".'' triggers split (after 3+ words)
        assert len(segs) == 2

    def test_empty_input_returns_empty(self):
        assert local_group_words([], []) == []


class TestLocalGroupFromSegments:
    def test_flatten_and_regroup(self):
        from models.transcript import TranscriptSegment

        seg = TranscriptSegment(
            start=0.0, end=2.0, text="hello world", speaker="SPEAKER_00",
            words=[
                WordTimestamp(word="hello", start=0.0, end=1.0),
                WordTimestamp(word="world", start=1.0, end=2.0),
            ],
        )
        out = local_group_from_segments([seg])
        assert len(out) >= 1
        assert any("hello" in s.text for s in out)


# ─── System instruction (anti-AI baseline + style preset + style note) ───

class TestBuildSystemInstruction:
    def test_baseline_rules_present_for_natural_default(self):
        from processors.translator.gemini_client import _build_system_instruction

        si = _build_system_instruction("English")
        # Anti-AI baseline rules
        assert "FUNCTIONALLY" in si
        assert "CONTEXT-AWARE" in si
        assert "RESTRUCTURE" in si
        assert "EXACTLY as-is" in si  # onomatopoeia rule preserved
        # Default preset
        assert "NATURAL" in si
        # Target language landed in the prompt
        assert "Target language: English." in si

    def test_formal_preset_swaps_block(self):
        from processors.translator.gemini_client import _build_system_instruction

        si = _build_system_instruction("Indonesian", style_preset="formal")
        assert "FORMAL" in si
        assert "no contractions" in si
        assert "NATURAL" not in si.split("Style preset: ")[1]

    def test_unknown_preset_falls_back_to_natural(self):
        from processors.translator.gemini_client import _build_system_instruction

        si = _build_system_instruction("English", style_preset="ridiculous")
        assert "NATURAL" in si

    def test_style_note_appended_additively_not_replaced(self):
        from processors.translator.gemini_client import _build_system_instruction

        si = _build_system_instruction(
            "English",
            style_preset="natural",
            style_note="keep JP honorifics like senpai/shishou raw",
        )
        # Preset block still present
        assert "NATURAL" in si
        # Note appended with the explicit delimiter
        assert "--- Additional user style note ---" in si
        assert "senpai/shishou" in si
        assert "--- End user style note ---" in si

    def test_blank_style_note_does_not_inject_delimiter(self):
        from processors.translator.gemini_client import _build_system_instruction

        si_blank = _build_system_instruction("English", style_note="   ")
        si_none = _build_system_instruction("English", style_note=None)
        assert "user style note" not in si_blank
        assert "user style note" not in si_none


# ─── Translator processor wires style + seed into Gemini payload ─────────

class TestTranslatorProcessorWiring:
    def test_constructor_normalises_unknown_preset(self):
        from processors.translator.processor import TranslatorProcessor

        p = TranslatorProcessor(
            target_language="id",
            style_preset="academic",
            style_note="  ",
        )
        assert p.style_preset == "natural"
        assert p.style_note is None

    def test_constructor_accepts_known_preset_and_note(self):
        from processors.translator.processor import TranslatorProcessor

        p = TranslatorProcessor(
            target_language="en",
            style_preset="formal",
            style_note="academic register",
        )
        assert p.style_preset == "formal"
        assert p.style_note == "academic register"

    def test_translate_prompt_no_longer_inlines_baseline_rules(self):
        """Anti-AI rules must live in systemInstruction, not in user message."""
        from processors.translator.gemini_client import _build_translate_prompt

        prompt = _build_translate_prompt(["Hello.", "World."], "Indonesian")
        assert "Indonesian" in prompt
        # Old baseline phrasing must not be duplicated in the user message
        assert "kyaaaa" not in prompt
        assert "ufufufu" not in prompt
        assert "natural speech style" not in prompt

