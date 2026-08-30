"""Tests for COMPILATION output mode: format_fit tiers + threshold selection.

Compilation mode extracts every moment clearing a quality bar and hands the
set to Premiere as one timeline, instead of publishing a diversified top-N.
Two behaviours must hold and are easy to regress:

  1. ``format_fit`` reads duration as a BUILDING BLOCK, so a good 45-90 s
     moment is ideal rather than "too short" (which previously suppressed
     it below the selection threshold).
  2. Selection keeps everything above the bar, chronologically, and
     resolves overlaps to the *better* clip rather than the earliest.
"""

from __future__ import annotations

from models.clip import Clip, ClipCandidate, ClipScore, HunterTag
from processors.clip_finder import selection
from processors.clip_finder.orchestrator import ClipFinder
from processors.clip_finder.scoring import (
    COMPILATION,
    STANDALONE,
    ClipScorer,
    _format_fit,
)
from processors.clip_finder.scoring_profiles import ScoringProfile


def _clip(start: float, end: float, q: float, title: str = "c") -> Clip:
    """Clip whose VTuber total is driven by a single quality knob."""
    return Clip(
        start=start,
        end=end,
        title=title,
        hunter=HunterTag.GENERAL,
        score=ClipScore(
            quotability=q,
            retention_hook=q,
            emotional_intensity=q,
            character_moment=q,
            interaction_dynamic=q,
            en_translatability=q,
            format_fit=10.0,
        ),
        score_profile="vtuber",
    )


# ─── format_fit is mode-aware ────────────────────────────────────────────────

class TestFormatFitCompilation:
    def test_typical_moment_lengths_are_ideal(self):
        for duration in (15.0, 45.0, 90.0, 120.0, 150.0):
            assert _format_fit(duration, COMPILATION) == 10.0

    def test_standalone_suppressed_those_same_lengths(self):
        # The regression this mode exists to fix: a 45-90 s moment scored
        # below max under standalone tiers, dragging its total down.
        assert _format_fit(45.0, STANDALONE) < 10.0
        assert _format_fit(90.0, STANDALONE) < 10.0

    def test_fragment_too_short_is_punished(self):
        assert _format_fit(5.0, COMPILATION) < 4.0
        assert _format_fit(0.0, COMPILATION) == 0.0

    def test_overlong_block_is_punished(self):
        assert _format_fit(300.0, COMPILATION) < 5.0
        assert _format_fit(600.0, COMPILATION) == 0.0

    def test_ramp_and_decay_are_monotonic(self):
        assert (
            _format_fit(10.0, COMPILATION)
            < _format_fit(12.0, COMPILATION)
            < _format_fit(15.0, COMPILATION)
        )
        assert (
            _format_fit(150.0, COMPILATION)
            > _format_fit(200.0, COMPILATION)
            > _format_fit(240.0, COMPILATION)
        )

    def test_always_within_zero_to_ten(self):
        for duration in range(0, 1200, 7):
            for fmt in (COMPILATION, STANDALONE):
                assert 0.0 <= _format_fit(float(duration), fmt) <= 10.0

    def test_standalone_is_the_default_and_unchanged(self):
        assert _format_fit(300.0) == 10.0
        assert _format_fit(30.0) == 7.0

    def test_scorer_threads_format_through(self):
        cand = ClipCandidate(start=0.0, end=90.0, title="x")
        comp = ClipScorer._deterministic_features(cand, [], 15.0, 600.0, COMPILATION)
        stand = ClipScorer._deterministic_features(cand, [], 15.0, 600.0, STANDALONE)
        assert comp["format_fit"] == 10.0
        assert stand["format_fit"] < 10.0


# ─── Threshold selection ─────────────────────────────────────────────────────

class TestSelectAboveThreshold:
    def test_keeps_everything_above_bar(self):
        strong, mid, weak = _clip(10, 100, 10, "s"), _clip(200, 290, 6, "m"), _clip(400, 490, 2, "w")
        kept = selection.select_above_threshold([weak, strong, mid], threshold=3.0)
        assert [c.title for c in kept] == ["s", "m"]

    def test_returns_chronological_not_score_order(self):
        late, early = _clip(500, 560, 9, "late"), _clip(50, 110, 7, "early")
        kept = selection.select_above_threshold([late, early], threshold=1.0)
        assert [c.title for c in kept] == ["early", "late"]

    def test_no_top_n_cap(self):
        many = [_clip(i * 200, i * 200 + 90, 9, f"m{i}") for i in range(30)]
        assert len(selection.select_above_threshold(many, threshold=3.0)) == 30

    def test_safety_max_count_still_applies(self):
        many = [_clip(i * 200, i * 200 + 90, 9, f"m{i}") for i in range(30)]
        kept = selection.select_above_threshold(many, threshold=3.0, max_count=10)
        assert len(kept) == 10

    def test_overlap_keeps_higher_scoring_not_earliest(self):
        # deduplicate_clips keeps the earliest; with no cap that is wrong.
        early_weak = _clip(100, 200, 5, "early_weak")
        late_strong = _clip(120, 220, 10, "late_strong")
        kept = selection.select_above_threshold([early_weak, late_strong], threshold=1.0)
        assert [c.title for c in kept] == ["late_strong"]

    def test_long_block_swallowing_tight_moment_is_one_beat(self):
        big, tight = _clip(100, 400, 6, "big"), _clip(150, 200, 10, "tight")
        kept = selection.select_above_threshold([big, tight], threshold=1.0)
        assert [c.title for c in kept] == ["tight"]

    def test_adjacent_non_overlapping_both_survive(self):
        kept = selection.select_above_threshold(
            [_clip(0, 90, 8, "a"), _clip(95, 185, 8, "b")], threshold=1.0
        )
        assert len(kept) == 2

    def test_empty_when_nothing_clears_bar(self):
        assert selection.select_above_threshold([_clip(0, 90, 2)], threshold=9.9) == []

    def test_empty_input(self):
        assert selection.select_above_threshold([], threshold=6.0) == []


# ─── Orchestrator routes the two modes ───────────────────────────────────────

class TestSelectFinalRouting:
    def _clips(self):
        return [_clip(10, 100, 10, "s"), _clip(200, 290, 6, "m"), _clip(400, 490, 2, "w")]

    def test_compilation_applies_quality_bar(self):
        out = ClipFinder()._select_final(
            self._clips(), clip_format=COMPILATION, threshold=3.0,
            max_count=None, profile=ScoringProfile.VTUBER,
            default_max=None, log_fn=None,
        )
        assert [c.title for c in out] == ["s", "m"]

    def test_compilation_reports_material_length(self):
        logs: list[str] = []
        ClipFinder()._select_final(
            self._clips(), clip_format=COMPILATION, threshold=3.0,
            max_count=None, profile=ScoringProfile.VTUBER,
            default_max=None, log_fn=logs.append,
        )
        assert any("min of material" in line for line in logs)

    def test_compilation_warns_when_nothing_qualifies(self):
        logs: list[str] = []
        ClipFinder()._select_final(
            [_clip(0, 90, 2)], clip_format=COMPILATION, threshold=9.9,
            max_count=None, profile=ScoringProfile.VTUBER,
            default_max=None, log_fn=logs.append,
        )
        assert any("WARNING" in line for line in logs)

    def test_standalone_without_cap_returns_all(self):
        # Legacy single-shot behaviour (ADR-0005 bug #2) must not change.
        out = ClipFinder()._select_final(
            self._clips(), clip_format=STANDALONE, threshold=None,
            max_count=None, profile=ScoringProfile.VTUBER,
            default_max=None, log_fn=None,
        )
        assert len(out) == 3

    def test_standalone_with_cap_limits_count(self):
        out = ClipFinder()._select_final(
            self._clips(), clip_format=STANDALONE, threshold=None,
            max_count=2, profile=ScoringProfile.VTUBER,
            default_max=None, log_fn=None,
        )
        assert len(out) == 2
