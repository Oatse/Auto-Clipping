"""Tests for processors.clip_finder.visual_signals.

The ``extract()`` coroutine shells out to FFmpeg, so we test only the
pure helpers (parser + segmentation) here. Integration of the full
extraction is covered manually against real video.
"""

from __future__ import annotations

import pytest

from models.clip import SignalEvent, SignalKind
from processors.clip_finder.visual_signals import (
    SCENE_THRESHOLD,
    _merge_close,
    _parse_showinfo,
    segments_between_cuts,
)


# ─── _parse_showinfo ─────────────────────────────────────────────────────────


def _ffmpeg_log(*lines: str) -> str:
    return "\n".join(lines) + "\n"


class TestParseShowinfo:
    def test_empty_input_returns_empty(self):
        assert _parse_showinfo("") == []

    def test_extracts_pts_and_score(self):
        text = _ffmpeg_log(
            "[Parsed_select_0 @ 0x...] n:0 pts_time:1.5 scene_score=0.652",
            "[Parsed_showinfo_1 @ 0x...] n:0 pts_time:1.5 ...",
        )
        result = _parse_showinfo(text)
        assert len(result) == 1
        t, score = result[0]
        assert t == pytest.approx(1.5)
        assert score == pytest.approx(0.652)

    def test_multiple_cuts(self):
        text = _ffmpeg_log(
            "[Parsed_select_0] n:0 pts_time:2.0 scene_score=0.5",
            "[Parsed_showinfo_1] n:0 pts_time:2.0 ...",
            "[Parsed_select_0] n:1 pts_time:6.5 scene_score=0.8",
            "[Parsed_showinfo_1] n:1 pts_time:6.5 ...",
        )
        result = _parse_showinfo(text)
        assert len(result) == 2
        assert [t for t, _ in result] == [pytest.approx(2.0), pytest.approx(6.5)]

    def test_missing_score_padded_with_threshold(self):
        """When showinfo lines outnumber select lines, pad with threshold."""
        text = _ffmpeg_log(
            "[Parsed_showinfo_1] n:0 pts_time:1.0",
            "[Parsed_showinfo_1] n:1 pts_time:2.0",
        )
        result = _parse_showinfo(text)
        assert len(result) == 2
        for _, score in result:
            assert score == pytest.approx(SCENE_THRESHOLD)

    def test_garbage_lines_ignored(self):
        text = _ffmpeg_log(
            "ffmpeg version 4.4 ...",
            "Stream mapping:",
            "[Parsed_showinfo_1] n:0 pts_time:3.7",
            "[Parsed_select_0] n:0 pts_time:3.7 scene_score=0.7",
        )
        assert _parse_showinfo(text) == [(pytest.approx(3.7), pytest.approx(0.7))]


# ─── _merge_close ────────────────────────────────────────────────────────────


class TestMergeClose:
    def test_empty_returns_empty(self):
        assert _merge_close([], min_gap=0.5) == []

    def test_single_passes_through(self):
        cuts = [(1.0, 0.6)]
        assert _merge_close(cuts, min_gap=0.5) == cuts

    def test_too_close_dropped(self):
        cuts = [(1.0, 0.6), (1.2, 0.7)]
        assert _merge_close(cuts, min_gap=0.5) == [(1.0, 0.6)]

    def test_far_apart_kept(self):
        cuts = [(1.0, 0.6), (3.0, 0.7), (3.6, 0.5)]
        assert _merge_close(cuts, min_gap=0.5) == [(1.0, 0.6), (3.0, 0.7), (3.6, 0.5)]

    def test_keeps_first_in_cluster(self):
        """First cut in a cluster wins — it marks the actual transition."""
        cuts = [(1.0, 0.5), (1.1, 0.9), (1.2, 0.95)]
        merged = _merge_close(cuts, min_gap=0.5)
        assert merged == [(1.0, 0.5)]


# ─── segments_between_cuts ───────────────────────────────────────────────────


def _scene_cut(t: float, score: float = 0.5) -> SignalEvent:
    return SignalEvent(
        kind=SignalKind.SCENE_CUT,
        start=t,
        end=t,
        intensity=score,
        label=f"cut@{t}",
    )


class TestSegmentsBetweenCuts:
    def test_no_cuts_returns_full_range(self):
        assert segments_between_cuts([], 10.0) == [(0.0, 10.0)]

    def test_zero_duration_returns_empty(self):
        assert segments_between_cuts([_scene_cut(2.0)], 0.0) == []

    def test_single_cut_splits_into_two(self):
        cuts = [_scene_cut(4.0)]
        assert segments_between_cuts(cuts, 10.0) == [(0.0, 4.0), (4.0, 10.0)]

    def test_two_cuts_split_into_three(self):
        cuts = [_scene_cut(2.0), _scene_cut(5.0)]
        result = segments_between_cuts(cuts, 10.0)
        assert result == [(0.0, 2.0), (2.0, 5.0), (5.0, 10.0)]

    def test_cuts_outside_range_ignored(self):
        cuts = [_scene_cut(-1.0), _scene_cut(0.0), _scene_cut(15.0)]
        assert segments_between_cuts(cuts, 10.0) == [(0.0, 10.0)]

    def test_non_scene_cut_signals_ignored(self):
        from models.clip import SignalKind as _SK
        audio = SignalEvent(kind=_SK.AUDIO_PEAK, start=4.0, end=4.5, intensity=0.5)
        assert segments_between_cuts([audio], 10.0) == [(0.0, 10.0)]

    def test_duplicate_cuts_dedup(self):
        cuts = [_scene_cut(3.0), _scene_cut(3.0)]
        assert segments_between_cuts(cuts, 10.0) == [(0.0, 3.0), (3.0, 10.0)]

    def test_cuts_returned_sorted(self):
        cuts = [_scene_cut(7.0), _scene_cut(2.0), _scene_cut(5.0)]
        result = segments_between_cuts(cuts, 10.0)
        # Sorted ascending by start
        starts = [s[0] for s in result]
        assert starts == sorted(starts)
