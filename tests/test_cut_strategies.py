"""Tests for processors.clip_finder.cut_strategies."""

from __future__ import annotations

import pytest

from models.clip import Clip, ClipScore, HighlightType, HunterTag
from processors.clip_finder.cut_strategies import (
    CutStrategy,
    StrategyPolicy,
    expand,
)


def _clip(start: float, end: float) -> Clip:
    return Clip(
        start=start,
        end=end,
        title="Test",
        reason="",
        highlight_type=HighlightType.UNSPECIFIED,
        hunter=HunterTag.GENERAL,
        score=ClipScore(),
    )


def _seg(start: float, end: float, text: str) -> dict:
    return {"start": start, "end": end, "text": text}


# ─── Strategy enum ───────────────────────────────────────────────────────────


class TestCutStrategyEnum:
    def test_coerce_passthrough(self):
        assert CutStrategy.coerce(CutStrategy.TIGHT) == CutStrategy.TIGHT

    def test_coerce_string(self):
        assert CutStrategy.coerce("tight") == CutStrategy.TIGHT

    def test_coerce_unknown_falls_back_to_base(self):
        assert CutStrategy.coerce("loop") == CutStrategy.BASE


# ─── No-op cases ─────────────────────────────────────────────────────────────


class TestNoOpCases:
    def test_empty_clips_returns_empty(self):
        assert expand([], [_seg(0.0, 5.0, "x")], strategies=(CutStrategy.TIGHT,)) == []

    def test_no_strategies_returns_input(self):
        clips = [_clip(10.0, 30.0)]
        out = expand(clips, [_seg(10.0, 13.0, "x")], strategies=())
        assert out == clips

    def test_no_transcript_returns_input(self):
        clips = [_clip(10.0, 30.0)]
        out = expand(clips, None, strategies=(CutStrategy.HOOKY,))
        assert out == clips


# ─── Tight strategy ──────────────────────────────────────────────────────────


class TestTightStrategy:
    def test_tight_shrinks_range(self):
        clips = [_clip(10.0, 30.0)]
        out = expand(
            clips,
            [_seg(10.0, 30.0, "speaker talking")],
            strategies=(CutStrategy.TIGHT,),
        )
        # base + tight derived
        assert len(out) == 2
        derived = [c for c in out if c.start != 10.0 or c.end != 30.0]
        assert len(derived) == 1
        # tight: trims head + tail by defaults (1.5 + 1.0)
        assert derived[0].start == pytest.approx(11.5)
        assert derived[0].end == pytest.approx(29.0)

    def test_tight_too_short_rejected(self):
        """When tight would shrink below min_duration, no derived Moment."""
        clips = [_clip(10.0, 15.0)]  # 5 s base
        policy = StrategyPolicy(
            tight_trim_head=1.5, tight_trim_tail=1.0, min_duration=5.0,
        )
        out = expand(
            clips,
            [_seg(10.0, 15.0, "x")],
            strategies=(CutStrategy.TIGHT,),
            policy=policy,
        )
        # only base survives
        assert len(out) == 1
        assert out[0].start == 10.0


# ─── Hooky strategy ──────────────────────────────────────────────────────────


class TestHookyStrategy:
    def test_hooky_shifts_when_hook_present(self):
        clips = [_clip(10.0, 30.0)]
        transcript = [_seg(10.0, 13.0, "and what is this")]
        out = expand(clips, transcript, strategies=(CutStrategy.HOOKY,))
        assert len(out) == 2
        derived = [c for c in out if c.start > 10.0]
        assert len(derived) == 1
        assert derived[0].end == 30.0

    def test_hooky_no_op_when_no_hook(self):
        clips = [_clip(10.0, 30.0)]
        transcript = [_seg(10.0, 13.0, "the speaker continues talking calmly")]
        out = expand(clips, transcript, strategies=(CutStrategy.HOOKY,))
        # Only base — hooky produced no new Moment.
        assert len(out) == 1


# ─── Context strategy ────────────────────────────────────────────────────────


class TestContextStrategy:
    def test_context_pads_back_to_topic_boundary(self):
        clips = [_clip(20.0, 40.0)]
        transcript = [
            _seg(5.0, 10.0, "earlier topic"),
            _seg(10.0, 20.0, "setup that leads into the moment"),
            _seg(20.0, 40.0, "the moment itself"),
        ]
        out = expand(clips, transcript, strategies=(CutStrategy.CONTEXT,))
        derived = [c for c in out if c.start < 20.0]
        assert len(derived) == 1
        # Earliest segment within lookback (default 20 s) is at 5.0 → start ≈ 5.0
        assert derived[0].start <= 10.0
        assert derived[0].end == 40.0

    def test_context_capped_by_lookback(self):
        clips = [_clip(50.0, 70.0)]
        transcript = [
            _seg(0.0, 10.0, "very early topic, far away"),
            _seg(40.0, 50.0, "directly leading"),
        ]
        out = expand(
            clips, transcript,
            strategies=(CutStrategy.CONTEXT,),
            policy=StrategyPolicy(context_lookback=15.0),
        )
        derived = [c for c in out if c.start < 50.0]
        assert len(derived) == 1
        # Segment at 0.0 is outside 15 s lookback; must use 40.0
        assert derived[0].start == 40.0

    def test_context_no_op_when_no_setup_segment(self):
        clips = [_clip(10.0, 30.0)]
        # No segment earlier than the Moment exists.
        transcript = [_seg(10.0, 30.0, "the moment itself")]
        out = expand(clips, transcript, strategies=(CutStrategy.CONTEXT,))
        assert len(out) == 1  # base only


# ─── Combined strategies + dedup ─────────────────────────────────────────────


class TestCombinedAndDedup:
    def test_all_three_strategies_at_once(self):
        clips = [_clip(20.0, 40.0)]
        transcript = [
            _seg(5.0, 10.0, "early topic"),
            _seg(10.0, 20.0, "setup"),
            _seg(20.0, 23.0, "and what is this"),
            _seg(23.0, 40.0, "rest of the moment"),
        ]
        out = expand(
            clips,
            transcript,
            strategies=(CutStrategy.TIGHT, CutStrategy.HOOKY, CutStrategy.CONTEXT),
        )
        # base + 3 derived (or fewer if any strategy was a no-op for this clip)
        assert 2 <= len(out) <= 4
        # base must always survive
        assert any(c.start == 20.0 and c.end == 40.0 for c in out)

    def test_duplicate_against_base_dropped(self):
        """If hooky lands on the same start as base, it must be dropped."""
        clips = [_clip(10.0, 30.0)]
        # Hook word at the very start → hooky would not move start meaningfully
        transcript = [_seg(10.0, 13.0, "what continues happening here")]
        out = expand(clips, transcript, strategies=(CutStrategy.HOOKY,))
        # base + maybe hooky if it found a later hook in window
        # but every Moment must be distinct
        seen = set()
        for c in out:
            key = (round(c.start, 1), round(c.end, 1))
            assert key not in seen, "duplicate Moments in output"
            seen.add(key)


# ─── Immutability ────────────────────────────────────────────────────────────


class TestImmutability:
    def test_input_clips_not_mutated(self):
        original = _clip(10.0, 30.0)
        expand(
            [original],
            [_seg(10.0, 13.0, "and what is this")],
            strategies=(CutStrategy.HOOKY, CutStrategy.TIGHT),
        )
        assert original.start == 10.0
        assert original.end == 30.0
