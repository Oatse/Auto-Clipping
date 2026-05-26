"""
processors/clip_finder/boundary.py — Boundary refinement for clip ranges.

Snaps a clip's start and end timestamps to nearby natural boundaries
detected from audio silence runs. The goal is for downloaded clips to
feel "professionally edited" — no mid-sentence cuts, no awkward dead
air at the head or tail.

Heuristics (in order of preference, with fallback to original):
  1. Snap start LATER to the end of the silence run that begins ≤10s
     before the clip's start.
  2. Snap end EARLIER to the start of the silence run that begins
     within ≤8s after the clip's end.
  3. Never let the refined range fall below `min_duration`.

If no SignalEvent stream is available, refine() is a no-op.
"""

from __future__ import annotations

from typing import Sequence

from models.clip import Clip, SignalEvent, SignalKind


HEAD_LOOKBACK = 10.0
TAIL_LOOKAHEAD = 8.0


def refine_boundaries(
    clips: Sequence[Clip],
    signals: Sequence[SignalEvent],
    *,
    min_duration: float = 5.0,
    transcript: Sequence[dict] | None = None,
    hook_optimizer_enabled: bool = True,
) -> list[Clip]:
    """Return a copy of `clips` with boundaries snapped where useful.

    Original Clip objects are NOT mutated. Inner `dead_air_timestamps`
    are also re-derived from silence signals that fall inside the
    refined range so they reflect ground-truth audio rather than LLM guess.

    When ``transcript`` is supplied and ``hook_optimizer_enabled`` is True
    (default), runs a second pass via ``hook_optimizer.apply`` to shift
    starts forward to a hook word (question / interjection / name-drop)
    inside the first 3 s of each Moment. The shift is bounded — see
    ADR-0003 and ``hook_optimizer.HookPolicy``.
    """
    if not signals:
        out = [_copy(c) for c in clips]
    else:
        silences = sorted(
            (s for s in signals if s.kind == SignalKind.AUDIO_SILENCE),
            key=lambda s: s.start,
        )
        if not silences:
            out = [_copy(c) for c in clips]
        else:
            out = []
            for clip in clips:
                new_start, new_end = _snap_range(clip.start, clip.end, silences)
                # Floor on duration
                if new_end - new_start < min_duration:
                    new_start, new_end = clip.start, clip.end

                clone = _copy(clip)
                clone.start = round(new_start, 3)
                clone.end = round(new_end, 3)
                clone.dead_air_timestamps = _silences_inside(
                    new_start, new_end, silences
                )
                out.append(clone)

    if hook_optimizer_enabled and transcript:
        # Lazy import keeps boundary.py importable in unit tests that
        # don't pull in the rest of clip_finder.
        from . import hook_optimizer
        out = hook_optimizer.apply(
            out,
            transcript,
            policy=hook_optimizer.HookPolicy(min_duration=min_duration),
        )

    return out


def _snap_range(
    start: float,
    end: float,
    silences: Sequence[SignalEvent],
) -> tuple[float, float]:
    new_start = start
    new_end = end

    # Snap start: pick the latest silence END that lies in [start - LOOKBACK, start]
    head_candidates = [
        s.end for s in silences
        if start - HEAD_LOOKBACK <= s.end <= start
    ]
    if head_candidates:
        new_start = max(head_candidates)

    # Snap end: pick the earliest silence START that lies in [end, end + LOOKAHEAD]
    tail_candidates = [
        s.start for s in silences
        if end <= s.start <= end + TAIL_LOOKAHEAD
    ]
    if tail_candidates:
        new_end = min(tail_candidates)

    if new_end <= new_start:
        return start, end
    return new_start, new_end


def _silences_inside(
    start: float, end: float, silences: Sequence[SignalEvent]
) -> list[float]:
    """Return midpoints of silence runs strictly inside [start, end]."""
    out: list[float] = []
    for s in silences:
        if s.start > start and s.end < end and s.duration >= 5.0:
            out.append(round((s.start + s.end) / 2.0, 3))
    return out


def _copy(clip: Clip) -> Clip:
    """Shallow clone of a Clip (mutates only top-level fields, not score)."""
    return Clip(
        start=clip.start,
        end=clip.end,
        title=clip.title,
        reason=clip.reason,
        highlight_type=clip.highlight_type,
        hunter=clip.hunter,
        dead_air_timestamps=list(clip.dead_air_timestamps),
        score=clip.score,
        rescued=clip.rescued,
        file_idx=clip.file_idx,
        filename=clip.filename,
        signals=list(clip.signals),
    )


__all__ = ["refine_boundaries"]
