"""Tests for processors.timing.natural_caption — Natural Caption Style pass.

Two passes covered:

1. ``_strip_trailing_micropunct`` — drops a single trailing ``.`` / ``,``
   / ``。`` / ``、`` while preserving ``?`` / ``!`` / ellipsis.
2. ``_split_long_segment`` — slices long segments into 2-3 word-aligned
   sub-segments, preferring punctuation-anchored cut points.

Plus end-to-end ``apply_natural_caption_style`` orchestration tests.
"""

from __future__ import annotations

from dataclasses import replace

from models.transcript import TranscriptSegment, WordTimestamp
from processors.timing import (
    DEFAULT_MAX_LINE_CHARS,
    apply_natural_caption_style,
)
from processors.timing.natural_caption import (
    _find_balanced_cuts,
    _split_long_segment,
    _strip_trailing_micropunct,
)


def _seg(start: float, end: float, text: str, words: list[tuple[str, float, float]]):
    """Build a TranscriptSegment with the given (word, start, end) tuples."""
    return TranscriptSegment(
        start=start,
        end=end,
        text=text,
        speaker="SPEAKER_00",
        words=[WordTimestamp(word=w, start=s, end=e) for w, s, e in words],
    )


# ─── _strip_trailing_micropunct ──────────────────────────────────────────


class TestStripTrailingMicropunct:
    def test_strips_single_period(self):
        assert _strip_trailing_micropunct("hello world.") == "hello world"

    def test_strips_single_comma(self):
        assert _strip_trailing_micropunct("yeah, okay,") == "yeah, okay"

    def test_strips_cjk_period(self):
        assert _strip_trailing_micropunct("こんにちは。") == "こんにちは"

    def test_strips_cjk_comma(self):
        assert _strip_trailing_micropunct("はい、") == "はい"

    def test_preserves_question_mark(self):
        assert _strip_trailing_micropunct("really?") == "really?"

    def test_preserves_exclamation(self):
        assert _strip_trailing_micropunct("amazing!") == "amazing!"

    def test_preserves_cjk_question(self):
        assert _strip_trailing_micropunct("本当？") == "本当？"

    def test_preserves_cjk_exclamation(self):
        assert _strip_trailing_micropunct("すごい！") == "すごい！"

    def test_preserves_ellipsis(self):
        # Trailing "..." is intentional trailing-off, not a sentence end.
        assert _strip_trailing_micropunct("trailing off...") == "trailing off..."

    def test_preserves_long_ellipsis(self):
        # 4+ dots also count as ellipsis.
        assert _strip_trailing_micropunct("really....") == "really...."

    def test_preserves_mid_punctuation(self):
        # Comma in the middle is a reading micro-pause — keep it.
        assert _strip_trailing_micropunct("hello, there") == "hello, there"

    def test_strips_only_trailing(self):
        # Inner comma stays; trailing period drops.
        assert _strip_trailing_micropunct("hello, world.") == "hello, world"

    def test_idempotent_on_clean_text(self):
        assert _strip_trailing_micropunct("hello world") == "hello world"

    def test_idempotent_after_strip(self):
        first = _strip_trailing_micropunct("hello.")
        second = _strip_trailing_micropunct(first)
        assert first == second == "hello"

    def test_empty_string(self):
        assert _strip_trailing_micropunct("") == ""

    def test_whitespace_only(self):
        assert _strip_trailing_micropunct("   ") == "   "

    def test_strips_one_at_a_time(self):
        # We strip one trailing micro-punct per call so a deliberate ".."
        # at the end (not a 3-dot ellipsis) loses one dot per pass.
        assert _strip_trailing_micropunct("wait..") == "wait."

    def test_strips_with_trailing_whitespace(self):
        assert _strip_trailing_micropunct("hello.  ") == "hello"


# ─── _find_balanced_cuts ─────────────────────────────────────────────────


class TestFindBalancedCuts:
    def test_two_lines_balanced(self):
        # Six 4-char words: "abcd abcd abcd abcd abcd abcd" — 29 chars total.
        # Target for 2 lines is 14.5; the cut after word 3 lands at 14
        # (3 words * 4 + 2 spaces) — closest possible.
        words = [WordTimestamp(word="abcd", start=0, end=1) for _ in range(6)]
        cuts = _find_balanced_cuts(words, 2)
        assert cuts == [3]

    def test_three_lines_balanced(self):
        # Nine equal words, 3-line split should land at thirds.
        words = [WordTimestamp(word="abc", start=0, end=1) for _ in range(9)]
        cuts = _find_balanced_cuts(words, 3)
        assert cuts == [3, 6]

    def test_prefers_punct_anchor(self):
        # Words at positions 0..5 with a comma at index 2 should pull the
        # 2-line cut to 3 (right after the comma) even when the pure
        # midpoint would otherwise pick 3 anyway. Verifies the bonus path.
        words = [
            WordTimestamp(word="aa", start=0, end=1),
            WordTimestamp(word="bb", start=1, end=2),
            WordTimestamp(word="cc,", start=2, end=3),
            WordTimestamp(word="dd", start=3, end=4),
            WordTimestamp(word="ee", start=4, end=5),
            WordTimestamp(word="ff", start=5, end=6),
        ]
        cuts = _find_balanced_cuts(words, 2)
        assert cuts == [3]

    def test_strictly_increasing_cuts(self):
        words = [WordTimestamp(word="x", start=i, end=i + 1) for i in range(5)]
        cuts = _find_balanced_cuts(words, 3)
        assert cuts == sorted(set(cuts)) == cuts
        assert all(c > 0 for c in cuts)
        assert all(c < len(words) for c in cuts)

    def test_one_word_per_line_fallback(self):
        # n_lines == n words → one word per line.
        words = [WordTimestamp(word="x", start=i, end=i + 1) for i in range(3)]
        cuts = _find_balanced_cuts(words, 3)
        assert cuts == [1, 2]


# ─── _split_long_segment ─────────────────────────────────────────────────


class TestSplitLongSegment:
    def test_short_segment_unchanged(self):
        seg = _seg(0, 1, "hi there", [("hi", 0, 0.5), ("there", 0.5, 1)])
        result = _split_long_segment(seg, max_chars=24, max_lines=3)
        assert len(result) == 1
        assert result[0] is seg

    def test_long_segment_splits_into_two(self):
        # 30-char text → above 24 threshold but ≤ 48 → 2 sub-segments.
        seg = _seg(
            0, 6,
            "this is a fairly longish line",
            [
                ("this", 0, 0.5),
                ("is", 0.5, 1),
                ("a", 1, 1.5),
                ("fairly", 1.5, 2.5),
                ("longish", 2.5, 4.5),
                ("line", 4.5, 6),
            ],
        )
        result = _split_long_segment(seg, max_chars=24, max_lines=3)
        assert len(result) == 2
        # All words preserved across sub-segments.
        all_words = [w for s in result for w in s.words]
        assert len(all_words) == 6
        assert all_words[0].word == "this"
        assert all_words[-1].word == "line"

    def test_very_long_segment_splits_into_three(self):
        # 50+ chars → 3 sub-segments.
        words_data = [
            ("alpha", 0, 0.5), ("bravo", 0.5, 1), ("charlie", 1, 1.5),
            ("delta", 1.5, 2), ("echo", 2, 2.5), ("foxtrot", 2.5, 3),
            ("golf", 3, 3.5), ("hotel", 3.5, 4), ("india", 4, 4.5),
        ]
        text = " ".join(w[0] for w in words_data)
        seg = _seg(0, 4.5, text, words_data)
        result = _split_long_segment(seg, max_chars=24, max_lines=3)
        assert len(result) == 3

    def test_split_preserves_speaker(self):
        seg = TranscriptSegment(
            start=0, end=3,
            text="this is a fairly longish line indeed",
            speaker="SPEAKER_02",
            words=[
                WordTimestamp(word="this", start=0, end=0.4),
                WordTimestamp(word="is", start=0.4, end=0.6),
                WordTimestamp(word="a", start=0.6, end=0.8),
                WordTimestamp(word="fairly", start=0.8, end=1.4),
                WordTimestamp(word="longish", start=1.4, end=2),
                WordTimestamp(word="line", start=2, end=2.5),
                WordTimestamp(word="indeed", start=2.5, end=3),
            ],
        )
        result = _split_long_segment(seg, max_chars=24, max_lines=3)
        assert len(result) >= 2
        for sub in result:
            assert sub.speaker == "SPEAKER_02"

    def test_split_preserves_position_overrides(self):
        seg = TranscriptSegment(
            start=0, end=2,
            text="this is a fairly longish line",
            speaker="SPEAKER_00",
            words=[
                WordTimestamp(word="this", start=0, end=0.3),
                WordTimestamp(word="is", start=0.3, end=0.5),
                WordTimestamp(word="a", start=0.5, end=0.7),
                WordTimestamp(word="fairly", start=0.7, end=1.2),
                WordTimestamp(word="longish", start=1.2, end=1.7),
                WordTimestamp(word="line", start=1.7, end=2),
            ],
            pos_x=20, pos_y=80, pos_override=True,
        )
        result = _split_long_segment(seg, max_chars=24, max_lines=3)
        assert len(result) >= 2
        for sub in result:
            assert sub.pos_x == 20
            assert sub.pos_y == 80
            assert sub.pos_override is True

    def test_split_timestamps_continuous(self):
        # Sub-segments should tile the original time range without gaps.
        seg = _seg(
            10, 16,
            "alpha bravo charlie delta echo foxtrot",
            [
                ("alpha", 10, 11), ("bravo", 11, 12), ("charlie", 12, 13),
                ("delta", 13, 14), ("echo", 14, 15), ("foxtrot", 15, 16),
            ],
        )
        result = _split_long_segment(seg, max_chars=24, max_lines=3)
        assert len(result) >= 2
        # First sub starts at original start, last sub ends at original end.
        assert result[0].start == 10
        assert result[-1].end == 16
        # Sub-segments are chronologically ordered.
        for k in range(len(result) - 1):
            assert result[k].end <= result[k + 1].start + 1e-6

    def test_split_skipped_when_no_words(self):
        seg = TranscriptSegment(
            start=0, end=5,
            text="x" * 60,  # very long but no words → can't split.
            speaker="SPEAKER_00",
            words=[],
        )
        result = _split_long_segment(seg, max_chars=24, max_lines=3)
        assert result == [seg]

    def test_split_skipped_when_one_word(self):
        seg = _seg(0, 5, "supercalifragilisticexpialidocious",
                   [("supercalifragilisticexpialidocious", 0, 5)])
        result = _split_long_segment(seg, max_chars=24, max_lines=3)
        assert result == [seg]

    def test_split_uses_translated_text_not_source_words(self):
        # Regression: when seg.text is the translated string but
        # seg.words[] still carries source-language tokens (the normal
        # state after Phase 2 — Gemini translates whole segments while
        # the per-word ElevenLabs timing is preserved), splitting must
        # derive sub-segment display text from seg.text. Before the
        # fix, sub-segments were built from " ".join(w.word ...) which
        # leaked the source language into the rendered subtitle.
        seg = TranscriptSegment(
            start=0, end=4,
            # Translated English text — drives the display tokens.
            text="She has so little R18 art it is basically nothing",
            speaker="SPEAKER_00",
            # Source-language Japanese kana (shape mirrors what
            # ElevenLabs returns for a JP source video).
            words=[
                WordTimestamp(word="あ", start=0.0, end=0.3),
                WordTimestamp(word="ま", start=0.3, end=0.5),
                WordTimestamp(word="り", start=0.5, end=0.8),
                WordTimestamp(word="に", start=0.8, end=1.1),
                WordTimestamp(word="も", start=1.1, end=1.4),
                WordTimestamp(word="R18", start=1.4, end=1.9),
                WordTimestamp(word="イ", start=1.9, end=2.2),
                WordTimestamp(word="ラ", start=2.2, end=2.5),
                WordTimestamp(word="ス", start=2.5, end=2.8),
                WordTimestamp(word="ト", start=2.8, end=3.1),
                WordTimestamp(word="が", start=3.1, end=3.4),
                WordTimestamp(word="少", start=3.4, end=3.7),
                WordTimestamp(word="な", start=3.7, end=4.0),
            ],
        )
        result = apply_natural_caption_style([seg])
        # Sub-segments must hold ENGLISH text, not reconstructed kana.
        for sub in result:
            assert any(c.isalpha() and ord(c) < 128 for c in sub.text), (
                f"sub-segment text leaked source language: {sub.text!r}"
            )
            # No CJK characters in the display text.
            assert not any(
                "\u3000" <= c <= "\u9fff" or "\uff00" <= c <= "\uffef"
                for c in sub.text
            ), f"sub-segment text contains CJK chars: {sub.text!r}"
        # Together the sub-segments cover the same translated tokens.
        joined = " ".join(s.text for s in result)
        # Allow the trailing-punct stripper to drop a final period.
        assert (
            joined == seg.text or joined == seg.text.rstrip(".,")
        ), f"sub-segment texts don't tile the translated text: {joined!r}"

    def test_split_keeps_segment_when_translated_text_too_short(self):
        # Defensive case: translated text has fewer tokens than the
        # number of sub-segments we would otherwise produce. Splitting
        # at word level would give us empty sub-segments. Keep whole.
        seg = TranscriptSegment(
            start=0, end=3,
            text="okay",  # 1 token only
            speaker="SPEAKER_00",
            words=[
                WordTimestamp(word="は", start=0, end=0.5),
                WordTimestamp(word="い", start=0.5, end=1),
                WordTimestamp(word="そ", start=1, end=1.5),
                WordTimestamp(word="う", start=1.5, end=2),
                WordTimestamp(word="で", start=2, end=2.5),
                WordTimestamp(word="す", start=2.5, end=3),
            ],
        )
        # Force the long-text path by making the (would-be) display text
        # exceed max_chars even though it has only 1 token. We test the
        # 0-token guard via a separate path.
        long_seg = replace(seg, text="okayokay" * 5)  # 40 chars, 1 token
        result = _split_long_segment(long_seg, max_chars=24, max_lines=3)
        # 1 token can't split into 2 sub-segments without an empty one.
        assert len(result) == 1
        assert result[0] is long_seg


# ─── apply_natural_caption_style — end-to-end ────────────────────────────


class TestApplyNaturalCaptionStyle:
    def test_empty_input(self):
        assert apply_natural_caption_style([]) == []

    def test_strips_trailing_period_text_and_word(self):
        seg = _seg(0, 1, "hello world.", [("hello", 0, 0.5), ("world.", 0.5, 1)])
        result = apply_natural_caption_style([seg])
        assert len(result) == 1
        assert result[0].text == "hello world"
        assert result[0].words[-1].word == "world"

    def test_preserves_question_mark(self):
        seg = _seg(0, 1, "really?", [("really?", 0, 1)])
        result = apply_natural_caption_style([seg])
        assert result[0].text == "really?"
        assert result[0].words[0].word == "really?"

    def test_split_then_strip_composed(self):
        # Long segment ends in a period — after split, only the LAST
        # sub-segment should have its trailing period stripped. Earlier
        # sub-segments don't have a trailing period to strip.
        seg = _seg(
            0, 3,
            "this is a fairly longish caption line.",
            [
                ("this", 0, 0.3), ("is", 0.3, 0.5), ("a", 0.5, 0.7),
                ("fairly", 0.7, 1.2), ("longish", 1.2, 1.7),
                ("caption", 1.7, 2.3), ("line.", 2.3, 3),
            ],
        )
        result = apply_natural_caption_style([seg])
        assert len(result) >= 2
        assert not result[-1].text.endswith(".")
        assert result[-1].words[-1].word == "line"

    def test_disabled_split_keeps_one_segment(self):
        seg = _seg(
            0, 2,
            "this is a fairly longish line.",
            [
                ("this", 0, 0.3), ("is", 0.3, 0.5), ("a", 0.5, 0.7),
                ("fairly", 0.7, 1.2), ("longish", 1.2, 1.7),
                ("line.", 1.7, 2),
            ],
        )
        result = apply_natural_caption_style([seg], split_long_segments=False)
        assert len(result) == 1
        assert result[0].text == "this is a fairly longish line"

    def test_disabled_strip_preserves_punct(self):
        seg = _seg(0, 1, "hello.", [("hello.", 0, 1)])
        result = apply_natural_caption_style([seg], drop_trailing_punct=False)
        assert result[0].text == "hello."
        assert result[0].words[0].word == "hello."

    def test_both_disabled_returns_input(self):
        seg = _seg(
            0, 3,
            "this is a fairly longish line.",
            [("this", 0, 0.5), ("is", 0.5, 1), ("a", 1, 1.5),
             ("fairly", 1.5, 2), ("longish", 2, 2.5), ("line.", 2.5, 3)],
        )
        result = apply_natural_caption_style(
            [seg], drop_trailing_punct=False, split_long_segments=False,
        )
        assert len(result) == 1
        assert result[0].text == "this is a fairly longish line."

    def test_idempotent(self):
        seg = _seg(
            0, 2,
            "alpha bravo charlie delta echo foxtrot golf.",
            [
                ("alpha", 0, 0.3), ("bravo", 0.3, 0.5),
                ("charlie", 0.5, 0.8), ("delta", 0.8, 1.1),
                ("echo", 1.1, 1.4), ("foxtrot", 1.4, 1.7),
                ("golf.", 1.7, 2),
            ],
        )
        first = apply_natural_caption_style([seg])
        second = apply_natural_caption_style(first)
        assert len(first) == len(second)
        for a, b in zip(first, second):
            assert a.text == b.text
            assert [w.word for w in a.words] == [w.word for w in b.words]

    def test_default_threshold_constant_exposed(self):
        assert DEFAULT_MAX_LINE_CHARS == 24

    def test_preserves_input_speaker_diversity(self):
        # Two segments, two different speakers — both survive.
        s1 = TranscriptSegment(
            start=0, end=1, text="hello.", speaker="SPEAKER_01",
            words=[WordTimestamp(word="hello.", start=0, end=1)],
        )
        s2 = TranscriptSegment(
            start=1, end=2, text="world,", speaker="SPEAKER_02",
            words=[WordTimestamp(word="world,", start=1, end=2)],
        )
        result = apply_natural_caption_style([s1, s2])
        assert len(result) == 2
        assert result[0].speaker == "SPEAKER_01"
        assert result[1].speaker == "SPEAKER_02"
        assert result[0].text == "hello"
        assert result[1].text == "world"
