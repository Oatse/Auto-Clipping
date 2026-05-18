"""Tests for processors.translator.recheck — word-level alignment recheck."""

from __future__ import annotations

from models.transcript import TranscriptSegment, WordTimestamp
from processors.translator.recheck import recheck_word_level_alignment


def _w(word, start, end):
    return WordTimestamp(word=word, start=start, end=end)


def _seg(start, end, speaker, text, words):
    return TranscriptSegment(
        start=start, end=end, text=text, speaker=speaker, words=words,
    )


class TestRecheckSnapBoundaries:
    def test_snap_segment_start_to_first_word(self):
        words = [_w("hi", 0.5, 1.0)]
        segs = [_seg(0.0, 1.0, "SPEAKER_00", "hi", words)]
        out = recheck_word_level_alignment(segs, words, ["SPEAKER_00"])
        assert out[0].start == 0.5

    def test_snap_segment_end_to_last_word(self):
        words = [_w("hi", 0.0, 0.8)]
        segs = [_seg(0.0, 5.0, "SPEAKER_00", "hi", words)]
        out = recheck_word_level_alignment(segs, words, ["SPEAKER_00"])
        assert out[0].end == 0.8


class TestRecheckDuplicateRemoval:
    def test_word_appearing_in_two_segments_is_deduped(self):
        # Same word in both segments by reference (rare, simulates Gemini bug)
        shared = _w("hello", 1.0, 1.5)
        original_words = [shared]
        segs = [
            _seg(0.0, 1.5, "SPEAKER_00", "hello", [shared]),
            _seg(1.5, 2.0, "SPEAKER_00", "hello", [shared]),
        ]
        out = recheck_word_level_alignment(segs, original_words, ["SPEAKER_00"])
        # Word should appear in exactly one segment after dedup
        total = sum(len(s.words) for s in out)
        assert total == 1, f"expected 1 word total, got {total}"


class TestRecheckMissingWordRecovery:
    def test_missing_word_inserted_into_adjacent_segment(self):
        # original ElevenLabs has 3 words: hello world bye
        original_words = [
            _w("hello", 0.0, 0.5),
            _w("world", 0.5, 1.0),
            _w("bye", 1.0, 1.5),
        ]
        # but Gemini only kept 2 in one segment
        kept = [original_words[0], original_words[1]]
        segs = [_seg(0.0, 1.0, "SPEAKER_00", "hello world", kept)]
        out = recheck_word_level_alignment(
            segs, original_words, ["SPEAKER_00", "SPEAKER_00", "SPEAKER_00"],
        )
        # Missing 'bye' must end up somewhere
        all_words = [w.word for s in out for w in s.words]
        assert "bye" in all_words


class TestRecheckSortAndOverlap:
    def test_segments_sorted_after_recheck(self):
        # Construct out-of-order segments
        segs = [
            _seg(2.0, 3.0, "SPEAKER_00", "late", [_w("late", 2.0, 3.0)]),
            _seg(0.0, 1.0, "SPEAKER_00", "early", [_w("early", 0.0, 1.0)]),
        ]
        original_words = [_w("early", 0.0, 1.0), _w("late", 2.0, 3.0)]
        out = recheck_word_level_alignment(
            segs, original_words, ["SPEAKER_00", "SPEAKER_00"],
        )
        assert out[0].start < out[1].start

    def test_segment_overlap_trimmed(self):
        original_words = [_w("a", 0.0, 1.0), _w("b", 0.9, 2.0)]
        segs = [
            _seg(0.0, 1.5, "SPEAKER_00", "a", [original_words[0]]),
            _seg(0.9, 2.0, "SPEAKER_00", "b", [original_words[1]]),
        ]
        out = recheck_word_level_alignment(
            segs, original_words, ["SPEAKER_00", "SPEAKER_00"],
        )
        # First segment must end before second starts
        assert out[0].end <= out[1].start


class TestRecheckEmpty:
    def test_no_segments_returns_input_unchanged(self):
        out = recheck_word_level_alignment([], [], [])
        assert out == []

    def test_no_original_words_returns_input_unchanged(self):
        segs = [_seg(0.0, 1.0, "SPEAKER_00", "x", [_w("x", 0.0, 1.0)])]
        out = recheck_word_level_alignment(segs, [], [])
        assert out is segs


class TestRecheckRestoreExactTimestamps:
    def test_drifted_timestamp_restored_when_within_tolerance(self):
        # Segment word slightly drifted from ElevenLabs source
        original = _w("hello", 1.0, 1.5)
        drifted = _w("hello", 1.05, 1.55)  # 50 ms drift, within 100 ms tolerance
        segs = [_seg(1.05, 1.55, "SPEAKER_00", "hello", [drifted])]
        out = recheck_word_level_alignment(segs, [original], ["SPEAKER_00"])
        # Should snap back to ElevenLabs source
        assert out[0].words[0].start == 1.0
        assert out[0].words[0].end == 1.5

    def test_far_drift_triggers_missing_word_recovery(self):
        # Drift > 100 ms — the lookup table can't match by (start,end) key,
        # so the original word is treated as "missing" and the recovery pass
        # inserts it into the segment.  The drifted word stays in place.
        # This is intentional: word identity is keyed by exact timestamps,
        # so a far-drifted word is effectively a different word.
        original = _w("hello", 1.0, 1.5)
        drifted = _w("hello", 1.5, 2.0)
        segs = [_seg(1.5, 2.0, "SPEAKER_00", "hello", [drifted])]
        out = recheck_word_level_alignment(segs, [original], ["SPEAKER_00"])
        # Both words end up in the segment after recovery
        word_starts = sorted(w.start for w in out[0].words)
        assert 1.0 in word_starts
        assert 1.5 in word_starts
