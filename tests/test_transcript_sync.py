"""Tests for web.services.transcript_sync.sync_segment_words_with_text."""

from __future__ import annotations

from models.transcript import TranscriptSegment, WordTimestamp
from web.services.transcript_sync import sync_segment_words_with_text


def _seg(start, end, text, words):
    return TranscriptSegment(
        start=start, end=end, text=text, speaker="SPEAKER_00",
        words=[WordTimestamp(**w) for w in words],
    )


class TestSyncSegmentWordsWithText:
    def test_text_unchanged_no_op(self):
        seg = _seg(0.0, 2.0, "hello world", [
            {"word": "hello", "start": 0.0, "end": 1.0},
            {"word": "world", "start": 1.0, "end": 2.0},
        ])
        sync_segment_words_with_text(seg)
        assert [w.word for w in seg.words] == ["hello", "world"]
        assert seg.words[0].start == 0.0
        assert seg.words[0].end == 1.0

    def test_same_count_text_change_updates_words_in_place(self):
        seg = _seg(0.0, 2.0, "halo dunia", [
            {"word": "hello", "start": 0.0, "end": 1.0},
            {"word": "world", "start": 1.0, "end": 2.0},
        ])
        sync_segment_words_with_text(seg)
        # Words text changed but timestamps preserved
        assert [w.word for w in seg.words] == ["halo", "dunia"]
        assert seg.words[0].start == 0.0
        assert seg.words[0].end == 1.0

    def test_different_count_redistributes_proportionally(self):
        seg = _seg(0.0, 3.0, "halo dunia indah", [
            {"word": "hello", "start": 0.0, "end": 1.5},
            {"word": "world", "start": 1.5, "end": 3.0},
        ])
        sync_segment_words_with_text(seg)
        # 3 new words, span 3.0 → 1.0 each
        assert len(seg.words) == 3
        assert seg.words[0].start == 0.0
        assert seg.words[0].end == 1.0
        assert seg.words[1].start == 1.0
        assert seg.words[1].end == 2.0
        assert seg.words[2].start == 2.0
        assert seg.words[2].end == 3.0

    def test_empty_text_no_op(self):
        seg = _seg(0.0, 1.0, "", [
            {"word": "hello", "start": 0.0, "end": 1.0},
        ])
        sync_segment_words_with_text(seg)
        # Original words unchanged
        assert seg.words[0].word == "hello"
