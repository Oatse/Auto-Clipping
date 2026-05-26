"""
processors/clip_finder/cut_strategies.py — Derive multiple Moments per base time-range.

A Cut Strategy applies a deterministic refinement rule to a base
time-range that came out of detection / boundary refinement, producing a
*new* Moment with a different start/end pair. Three named strategies
ship in v1:

  - ``tight``   — head and tail trimmed toward the punchline. The Moment
    shrinks; mid-time is preserved.
  - ``hooky``   — start snapped to the first hook word in a ±3 s
    look-ahead window (uses the same lexicon as ``hook_optimizer``).
  - ``context`` — start padded back to the previous topic boundary
    (capped at +20 s) so the Moment carries setup before the payoff.

Cardinality contract (CONTEXT.md):
  - Each strategy still produces 1 Moment → 1 Clip.
  - One *base* time-range can fan out into N Moments (one per strategy).
  - Duplicates against the base or other strategies are removed before
    return.

Why a separate module: this is the seam where 1 base time-range becomes
a small set of Moments. Keeping it isolated lets us add new strategies
later (e.g. ``trailer`` for longer cuts, ``loop`` for repeating Vines)
without touching the orchestrator.

ADR-0003 contract: strategies run *after* boundary refinement and
*before* scoring. Each derived Moment is scored independently so the UI
can rank them by score even within the same base.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Sequence

from models.clip import Clip

from . import hook_optimizer


class CutStrategy(str, Enum):
    """Named cut strategy. The Job stores a list to enable fan-out."""

    BASE = "base"            # the unmodified Moment from detection
    TIGHT = "tight"
    HOOKY = "hooky"
    CONTEXT = "context"

    @classmethod
    def coerce(cls, value: object) -> "CutStrategy":
        if isinstance(value, CutStrategy):
            return value
        try:
            return cls(str(value).lower())
        except (ValueError, AttributeError):
            return cls.BASE


@dataclass(frozen=True)
class StrategyPolicy:
    """Tunables for cut strategies. Defaults match the ADR-0003 contract."""

    # tight: trim this much off head AND tail
    tight_trim_head: float = 1.5
    tight_trim_tail: float = 1.0
    # hooky: same window as hook_optimizer
    hooky_window: float = 3.0
    # context: how far back to look for a topic boundary
    context_lookback: float = 20.0
    # absolute minimum Moment length after derivation
    min_duration: float = 5.0
    # tolerance for "is this derived Moment a duplicate of another?"
    # Tight; we only want to drop near-identical cuts, not push two
    # genuinely different strategies into one.
    dedup_tolerance: float = 0.5


_DEFAULT_POLICY = StrategyPolicy()


# ─── Public API ──────────────────────────────────────────────────────────────


def expand(
    clips: Sequence[Clip],
    transcript: Sequence[dict] | None,
    *,
    strategies: Sequence[CutStrategy] = (),
    policy: StrategyPolicy = _DEFAULT_POLICY,
) -> list[Clip]:
    """Return ``clips`` plus extra Moments derived per Cut Strategy.

    ``BASE`` is included automatically — every base time-range survives
    even when downstream strategies do not. Duplicate Moments (within
    ``min_duration`` snap tolerance) are dropped after expansion.

    The original ``clips`` are NOT mutated. When ``strategies`` is empty
    or ``transcript`` is missing, the input is returned unchanged.
    """
    if not clips:
        return []
    if not strategies:
        return list(clips)
    if not transcript:
        return list(clips)

    expanded: list[Clip] = []

    for base in clips:
        # The base Moment always survives.
        expanded.append(base)

        for strat in strategies:
            if strat == CutStrategy.BASE:
                continue
            derived = _apply_strategy(
                strategy=strat,
                base=base,
                transcript=transcript,
                policy=policy,
            )
            if derived is None:
                continue
            if not _is_distinct(derived, expanded, min_distance=policy.dedup_tolerance):
                continue
            expanded.append(derived)

    return expanded


# ─── Strategy implementations ────────────────────────────────────────────────


def _apply_strategy(
    *,
    strategy: CutStrategy,
    base: Clip,
    transcript: Sequence[dict],
    policy: StrategyPolicy,
) -> Clip | None:
    """Dispatch to the correct strategy. Returns None when not applicable."""
    if strategy == CutStrategy.TIGHT:
        return _tight(base, policy)
    if strategy == CutStrategy.HOOKY:
        return _hooky(base, transcript, policy)
    if strategy == CutStrategy.CONTEXT:
        return _context(base, transcript, policy)
    return None


def _tight(base: Clip, policy: StrategyPolicy) -> Clip | None:
    """Trim ``tight_trim_head`` from start and ``tight_trim_tail`` from end."""
    new_start = base.start + policy.tight_trim_head
    new_end = base.end - policy.tight_trim_tail
    if new_end - new_start < policy.min_duration:
        return None
    return _clone(base, start=new_start, end=new_end)


def _hooky(
    base: Clip,
    transcript: Sequence[dict],
    policy: StrategyPolicy,
) -> Clip | None:
    """Reuse hook_optimizer to shift start to the first hook in the window."""
    hook_policy = hook_optimizer.HookPolicy(
        window_seconds=policy.hooky_window,
        min_duration=policy.min_duration,
        enabled=True,
    )
    shifted = hook_optimizer.apply([base], transcript, policy=hook_policy)
    if not shifted:
        return None
    candidate = shifted[0]
    # Optimizer is a no-op when no hook is found; reject duplicates.
    if abs(candidate.start - base.start) < 0.01:
        return None
    return candidate


def _context(
    base: Clip,
    transcript: Sequence[dict],
    policy: StrategyPolicy,
) -> Clip | None:
    """Pad ``start`` back to the previous topic boundary, capped at lookback.

    A "topic boundary" here is a transcript segment whose start time is
    earlier than ``base.start`` but within ``context_lookback`` seconds.
    We pick the *earliest* such segment so the Moment opens with the
    most setup the policy permits.
    """
    cutoff = max(0.0, base.start - policy.context_lookback)
    candidate_start: float | None = None
    for seg in transcript:
        if not isinstance(seg, dict):
            continue
        seg_start = float(seg.get("start", 0.0))
        if seg_start >= base.start:
            break  # transcript is ordered; once we pass base.start we stop
        if seg_start < cutoff:
            continue
        if candidate_start is None or seg_start < candidate_start:
            candidate_start = seg_start

    if candidate_start is None or candidate_start >= base.start - 1.0:
        # No useful context to add (less than 1 s of pad-back available).
        return None

    return _clone(base, start=candidate_start, end=base.end)


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _is_distinct(
    candidate: Clip,
    existing: Sequence[Clip],
    *,
    min_distance: float,
) -> bool:
    """True when ``candidate`` differs from every Moment in ``existing``.

    Two Moments are "the same" when both their start AND end are within
    ``min_distance`` seconds. This catches the case where ``hooky``
    happens to land on the same word as ``base``.
    """
    for prev in existing:
        if (
            abs(prev.start - candidate.start) < min_distance
            and abs(prev.end - candidate.end) < min_distance
        ):
            return False
    return True


def _clone(base: Clip, *, start: float, end: float) -> Clip:
    """Shallow clone of ``base`` with new start/end. Mirrors boundary._copy."""
    return Clip(
        start=round(start, 3),
        end=round(end, 3),
        title=base.title,
        reason=base.reason,
        highlight_type=base.highlight_type,
        hunter=base.hunter,
        dead_air_timestamps=list(base.dead_air_timestamps),
        score=base.score,
        rescued=base.rescued,
        file_idx=base.file_idx,
        filename=base.filename,
        signals=list(base.signals),
    )


__all__ = ["CutStrategy", "StrategyPolicy", "expand"]
