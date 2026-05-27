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


# ─── Recheck guard against invalid source words ──────────────────────────
#
# Regression coverage for the third defense-in-depth layer of the
# ElevenLabs zero-duration cluster bug.  Once commit 1 (STT-floor) and
# commit 2 (cluster redistribution) have repaired the timestamps, the
# recheck pass MUST NOT undo that work by snapping the word back to the
# original ElevenLabs source — which still has start == end.
#
# The guard: if a candidate "source word" has a non-positive duration,
# treat it as invalid and refuse to snap to it.

class TestRecheckInvalidSourceGuard:
    """``_restore_exact_timestamps`` must reject zero-duration source words."""

    def test_zero_duration_source_does_not_overwrite_repaired_word(self):
        # Source word from ElevenLabs is broken (start == end).
        # The translated segment carries the post-sanitize, repaired word.
        # The recheck pass must NOT pull the word back to the broken source.
        broken_source = _w("使", 6.41, 6.41)
        repaired = _w("使", 6.41, 6.46)  # post-STT-floor + cluster redistribute
        segs = [_seg(6.41, 6.46, "SPEAKER_00", "使", [repaired])]
        out = recheck_word_level_alignment(
            segs, [broken_source], ["SPEAKER_00"],
        )
        # The repaired word must keep its positive duration.
        assert out[0].words[0].end > out[0].words[0].start, (
            f"recheck snapped repaired word back to broken source: "
            f"start={out[0].words[0].start}, end={out[0].words[0].end}"
        )

    def test_valid_source_still_restored(self):
        # Sanity check: the guard must NOT block legitimate snap-back
        # when the source word has a real, positive duration.
        original = _w("hello", 1.0, 1.5)
        drifted = _w("hello", 1.03, 1.53)  # 30 ms drift
        segs = [_seg(1.03, 1.53, "SPEAKER_00", "hello", [drifted])]
        out = recheck_word_level_alignment(segs, [original], ["SPEAKER_00"])
        assert out[0].words[0].start == 1.0
        assert out[0].words[0].end == 1.5

    def test_negative_duration_source_rejected(self):
        # Defensive: a source word with end < start is also invalid.
        broken_source = _w("oops", 2.0, 1.5)
        repaired = _w("oops", 2.02, 2.07)
        segs = [_seg(2.02, 2.07, "SPEAKER_00", "oops", [repaired])]
        out = recheck_word_level_alignment(
            segs, [broken_source], ["SPEAKER_00"],
        )
        assert out[0].words[0].end > out[0].words[0].start

    def test_mikovsgundam_cluster_recheck_preserves_distribution(self):
        # End-to-end: simulate the post-sanitizer state for the kanji cluster
        # at t=6.41 — every word has a distinct start, valid duration.
        # Source words from ElevenLabs all share start == end == 6.41.
        # After recheck, the cluster MUST keep its distinct starts.
        kanji = list("使えるのかな？使えるのかな")
        broken_source = [_w(c, 6.41, 6.41) for c in kanji]
        # Repaired cluster: each word gets a 20ms stride
        repaired = [
            _w(c, round(6.41 + i * 0.02, 3), round(6.43 + i * 0.02, 3))
            for i, c in enumerate(kanji)
        ]
        segs = [_seg(repaired[0].start, repaired[-1].end,
                     "SPEAKER_00", "".join(kanji), repaired)]
        out = recheck_word_level_alignment(
            segs, broken_source, ["SPEAKER_00"] * len(kanji),
        )
        starts = [w.start for w in out[0].words]
        assert len(set(starts)) == len(kanji), (
            f"cluster collapsed by recheck: {starts}"
        )
