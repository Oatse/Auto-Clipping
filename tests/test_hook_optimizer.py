"""Tests for processors.clip_finder.hook_optimizer."""

from __future__ import annotations

import pytest

from models.clip import Clip, ClipScore, HighlightType, HunterTag
from processors.clip_finder.hook_optimizer import HookPolicy, apply


def _clip(start: float, end: float) -> Clip:
    """Build a minimal Clip for boundary tests."""
    return Clip(
        start=start,
        end=end,
        title="Test Moment",
        reason="",
        highlight_type=HighlightType.UNSPECIFIED,
        hunter=HunterTag.GENERAL,
        score=ClipScore(),
    )


def _seg(start: float, end: float, text: str) -> dict:
    """Mirror the transcript shape used everywhere in clip_finder."""
    return {"start": start, "end": end, "text": text}


# ─── Disabled / no-op cases ──────────────────────────────────────────────────


class TestNoOpCases:
    def test_disabled_policy_returns_input(self):
        clips = [_clip(10.0, 30.0)]
        transcript = [_seg(10.0, 13.0, "wait what no way")]
        out = apply(clips, transcript, policy=HookPolicy(enabled=False))
        assert out[0].start == 10.0

    def test_empty_transcript_no_op(self):
        clips = [_clip(10.0, 30.0)]
        out = apply(clips, [])
        assert out[0].start == 10.0

    def test_none_transcript_no_op(self):
        clips = [_clip(10.0, 30.0)]
        out = apply(clips, None)
        assert out[0].start == 10.0

    def test_empty_clips_returns_empty(self):
        assert apply([], [_seg(0.0, 5.0, "what")]) == []

    def test_no_hook_in_window_keeps_start(self):
        """When the window has no hook word, start is unchanged."""
        clips = [_clip(10.0, 30.0)]
        transcript = [_seg(10.0, 13.0, "the speaker continues talking calmly")]
        out = apply(clips, transcript)
        assert out[0].start == 10.0


# ─── Hook detection — happy path ─────────────────────────────────────────────


class TestHookDetection:
    def test_question_word_shifts_start_forward(self):
        """A 'what' inside the window should shift start to that word."""
        clips = [_clip(10.0, 30.0)]
        # Single segment 10s-13s, words: ["um", "what", "is", "this"]
        transcript = [_seg(10.0, 13.0, "um what is this")]
        out = apply(clips, transcript)
        # Words distributed: 10.0, 10.75, 11.5, 12.25
        # "what" at index 1 → t=10.75
        assert out[0].start > 10.0
        assert out[0].start < 13.0

    def test_indonesian_question_word_recognised(self):
        clips = [_clip(10.0, 30.0)]
        transcript = [_seg(10.0, 13.0, "jadi apa maksudmu")]
        out = apply(clips, transcript)
        assert out[0].start > 10.0

    def test_interjection_shifts_start(self):
        clips = [_clip(10.0, 30.0)]
        transcript = [_seg(10.0, 13.0, "and then bro look at this")]
        out = apply(clips, transcript)
        assert out[0].start > 10.0

    def test_all_caps_emphasis_is_hook(self):
        clips = [_clip(10.0, 30.0)]
        transcript = [_seg(10.0, 13.0, "and STOP what is this")]
        out = apply(clips, transcript)
        assert out[0].start > 10.0


# ─── Bounds — never break invariants ─────────────────────────────────────────


class TestBoundsInvariants:
    def test_never_shifts_backward(self):
        clips = [_clip(10.0, 30.0)]
        transcript = [_seg(8.0, 11.0, "what no way")]  # "what" at t≈8.0, before start
        out = apply(clips, transcript)
        # Hook word lives BEFORE start, must not be picked.
        assert out[0].start == 10.0

    def test_never_shifts_past_window(self):
        """Hook outside ±3 s window must be ignored."""
        clips = [_clip(10.0, 30.0)]
        # Hook at 14.5 s (past 3 s window from start=10.0)
        transcript = [
            _seg(10.0, 14.0, "the speaker continues talking calmly here"),
            _seg(14.0, 16.0, "what is this"),
        ]
        out = apply(clips, transcript, policy=HookPolicy(window_seconds=3.0))
        # No hook in [10.0, 13.0], must keep start.
        assert out[0].start == 10.0

    def test_min_duration_floor_respected(self):
        """Shifting that breaks min_duration must be rejected."""
        # Moment is 10s-15s (5s long); shifting forward past 11s breaks 5s floor.
        clips = [_clip(10.0, 15.0)]
        transcript = [_seg(10.0, 13.0, "hmm okay what about this thing")]
        out = apply(clips, transcript, policy=HookPolicy(window_seconds=3.0, min_duration=5.0))
        # The latest hook in window would shift past the floor → no shift.
        assert out[0].start == 10.0

    def test_first_word_skipped(self):
        """Word at index 0 is the existing start; we want a *better* anchor."""
        clips = [_clip(10.0, 30.0)]
        # First word IS the hook, but we shouldn't claim a non-shift as a shift.
        transcript = [_seg(10.0, 13.0, "what is going on here")]
        out = apply(clips, transcript)
        # start may shift, but never to 10.0 (the original start)
        assert out[0].start >= 10.0


# ─── Multi-clip + immutability ───────────────────────────────────────────────


class TestMultiClipImmutability:
    def test_multiple_clips_processed_independently(self):
        clip_a = _clip(10.0, 30.0)
        clip_b = _clip(50.0, 70.0)
        transcript = [
            _seg(10.0, 13.0, "and what is this"),
            _seg(50.0, 53.0, "the speaker continues talking calmly"),
        ]
        out = apply([clip_a, clip_b], transcript)
        assert out[0].start > clip_a.start  # shifted
        assert out[1].start == clip_b.start  # unshifted

    def test_input_clips_not_mutated(self):
        original = _clip(10.0, 30.0)
        transcript = [_seg(10.0, 13.0, "and what is this")]
        apply([original], transcript)
        assert original.start == 10.0  # original untouched

    def test_score_preserved_on_shifted_clip(self):
        original = Clip(
            start=10.0, end=30.0, title="t",
            score=ClipScore(retention_hook=8.5, emotional_intensity=7.0),
        )
        transcript = [_seg(10.0, 13.0, "and what is this")]
        out = apply([original], transcript)
        assert out[0].score.retention_hook == 8.5
        assert out[0].score.emotional_intensity == 7.0
