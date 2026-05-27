"""Tests for processors.stt.elevenlabs — ElevenLabs Scribe response parsing.

The Scribe v1 API occasionally returns ``start == end`` for word entries,
particularly on CJK kanji runs.  We rely on the STT layer to normalize
these to a small positive duration before the rest of the pipeline runs,
so the sanitizer never has to invent a duration from nothing.
"""

from __future__ import annotations

from processors.stt.elevenlabs import ElevenLabsSttEngine


def _engine() -> ElevenLabsSttEngine:
    """Build an engine with a dummy API key so __init__ passes validation."""
    return ElevenLabsSttEngine(api_keys=["dummy-key"])


def _word(text: str, start: float, end: float, speaker: str = "speaker_0") -> dict:
    """Build a single word dict shaped like ElevenLabs Scribe response."""
    return {
        "type": "word",
        "text": text,
        "start": start,
        "end": end,
        "speaker_id": speaker,
    }


# ─── _flush_speaker_turn — defensive duration normalization ──────────────


class TestFlushSpeakerTurnNormalizesZeroDuration:
    """Regression tests for the ElevenLabs zero-duration word bug.

    Scribe v1 occasionally returns ``start == end`` for short CJK runs
    (single-mora kanji, expressive interjections).  Without normalization
    those zero-duration words pass straight through to the sanitizer,
    where ``_fix_same_speaker_word_overlaps`` then drags every neighbour's
    ``end`` back to the cluster's ``start`` — which collapses the entire
    surrounding segment to zero duration in the editor timeline.

    The fix is to give every word at least a tiny floor duration here so
    the sanitizer's later cluster-redistribution pass has something to
    work with.
    """

    def test_zero_duration_word_normalized_to_positive_end(self):
        # Single word with start == end (the broken Scribe shape)
        segments: list = []
        engine = _engine()
        engine._flush_speaker_turn(
            segments=segments,
            current_words=[_word("使", 6.41, 6.41)],
            current_speaker="speaker_0",
            speaker_detection=True,
        )
        assert len(segments) == 1
        word = segments[0].words[0]
        assert word.end > word.start, (
            f"zero-duration word not normalized: start={word.start}, "
            f"end={word.end} (expected end > start)"
        )

    def test_normalized_end_does_not_overshoot(self):
        # Floor duration should be small (≤ 100 ms) so we don't manufacture
        # a fake duration that crowds out the real next word.
        segments: list = []
        engine = _engine()
        engine._flush_speaker_turn(
            segments=segments,
            current_words=[_word("使", 6.41, 6.41)],
            current_speaker="speaker_0",
            speaker_detection=True,
        )
        word = segments[0].words[0]
        assert word.end - word.start <= 0.1, (
            f"floor duration too aggressive: {word.end - word.start}s"
        )

    def test_valid_duration_word_unchanged(self):
        # A word with a valid duration must pass through untouched —
        # we only synthesize when the data is broken.
        segments: list = []
        engine = _engine()
        engine._flush_speaker_turn(
            segments=segments,
            current_words=[_word("hello", 1.0, 1.5)],
            current_speaker="speaker_0",
            speaker_detection=True,
        )
        word = segments[0].words[0]
        assert word.start == 1.0
        assert word.end == 1.5

    def test_negative_duration_word_normalized(self):
        # Defense in depth: end < start would produce negative duration.
        segments: list = []
        engine = _engine()
        engine._flush_speaker_turn(
            segments=segments,
            current_words=[_word("oops", 2.0, 1.5)],
            current_speaker="speaker_0",
            speaker_detection=True,
        )
        word = segments[0].words[0]
        assert word.end > word.start

    def test_cjk_cluster_each_word_gets_positive_duration(self):
        # The MIKOvsGUNDAM scenario: 13 kanji at the same anchor t=6.41,
        # each with start==end.  After the STT-level normalization every
        # word should have end > start; the sanitizer pass added in
        # commit 2 will be responsible for redistributing them.
        cluster = [_word(c, 6.41, 6.41) for c in "使えるのかな？使えるのかな"]
        segments: list = []
        engine = _engine()
        engine._flush_speaker_turn(
            segments=segments,
            current_words=cluster,
            current_speaker="speaker_0",
            speaker_detection=True,
        )
        for w in segments[0].words:
            assert w.end > w.start, (
                f"cluster word not normalized: word='{w.word}' "
                f"start={w.start} end={w.end}"
            )
