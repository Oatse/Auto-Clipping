"""
processors/clip_finder/selection.py — Picking which scored clips to keep.

Two selection philosophies live here, one per output mode:

  ``select_top_clips``       STANDALONE mode. Picks a fixed-size best
                             subset balancing score and diversity.

  ``select_above_threshold`` COMPILATION mode. Keeps *every* moment that
                             clears a quality bar, in chronological order,
                             because the moments are assembled into one
                             long video rather than published separately.
"""

from __future__ import annotations

from typing import Sequence

from models.clip import Clip, HunterTag

from .scoring_profiles import ScoringProfile


def _profile_total(clip: Clip, profile: ScoringProfile | str | None) -> float:
    """Resolve the score total for a clip under the requested profile.

    ADR-0005: when ``profile`` is None, fall back to the Clip's own
    ``score_profile`` field (set at scoring time by the orchestrator).
    Falls all the way back to ``ClipScore.total`` (VTuber legacy) when
    neither is available so unit tests that build bare ``Clip``
    objects keep working.
    """
    if profile is None:
        profile = getattr(clip, "score_profile", None) or ScoringProfile.VTUBER
    return clip.score.total_for(profile)


def _profile_sort_key(clip: Clip, profile: ScoringProfile | str | None):
    """Ranking key: clamped total first, raw pre-clamp total as tie-break.

    Several outstanding clips can all saturate the display total at 10.0.
    Without a tie-breaker the diversity selector — not quality — decides
    which of them wins the top slots. The raw pre-clamp sum keeps them in
    genuine quality order while the user still sees the clamped 10.0
    (VTuber-refocus Step 0).
    """
    if profile is None:
        profile = getattr(clip, "score_profile", None) or ScoringProfile.VTUBER
    return (
        clip.score.total_for(profile),
        clip.score.total_for(profile, clamp=False),
    )


def select_top_clips(
    clips: Sequence[Clip],
    *,
    max_count: int = 12,
    duration_budget: float | None = None,
    diversify_tags: bool = True,
    timeline_buckets: int = 6,
    profile: ScoringProfile | str | None = None,
) -> list[Clip]:
    """Select up to `max_count` clips balancing score and diversity.

    Parameters
    ----------
    max_count : maximum number of clips to keep
    duration_budget : if set, sum of selected durations must not exceed
        this many seconds (useful when assembling compilation videos)
    diversify_tags : enforce hunter-tag diversity using a soft penalty
    timeline_buckets : split the source video into N buckets and try to
        cover each bucket before returning to a popular one
    profile : Scoring Profile to rank by — when omitted, each clip's
        own ``score_profile`` field is used (ADR-0005)
    """
    if not clips:
        return []

    sorted_clips = sorted(
        clips, key=lambda c: _profile_sort_key(c, profile), reverse=True
    )
    if not sorted_clips:
        return []

    video_end = max(c.end for c in sorted_clips)
    bucket_size = max(1.0, video_end / timeline_buckets)

    selected: list[Clip] = []
    used_buckets: set[int] = set()
    used_tags: dict[HunterTag, int] = {}
    total_duration = 0.0

    # Pass 1: greedy with diversification penalties — prefer unseen tags + buckets
    for clip in sorted_clips:
        if len(selected) >= max_count:
            break
        bucket = int(clip.start // bucket_size)
        if duration_budget is not None and total_duration + clip.duration > duration_budget:
            continue
        # In pass 1, reject if either dim is already represented
        if diversify_tags and used_tags.get(clip.hunter, 0) >= 1 and clip.hunter != HunterTag.GENERAL:
            continue
        if bucket in used_buckets:
            continue
        selected.append(clip)
        used_buckets.add(bucket)
        used_tags[clip.hunter] = used_tags.get(clip.hunter, 0) + 1
        total_duration += clip.duration

    # Pass 2: fill remaining slots ignoring tag/bucket constraints, still budget-aware
    if len(selected) < max_count:
        for clip in sorted_clips:
            if clip in selected:
                continue
            if len(selected) >= max_count:
                break
            if duration_budget is not None and total_duration + clip.duration > duration_budget:
                continue
            selected.append(clip)
            total_duration += clip.duration

    selected.sort(key=lambda c: c.start)
    return selected


def select_above_threshold(
    clips: Sequence[Clip],
    *,
    threshold: float = 6.0,
    max_count: int = 60,
    overlap_ratio: float = 0.4,
    profile: ScoringProfile | str | None = None,
) -> list[Clip]:
    """Keep every moment scoring at or above ``threshold``, chronologically.

    COMPILATION mode. There is no "best N" here — the moments become
    building blocks in one long video, so the quality bar decides what
    survives and the total runtime simply follows.

    Overlap resolution differs from ``deduplicate_clips`` on purpose. That
    helper keeps whichever clip starts *earliest*, which is the wrong rule
    once nothing caps the list: two overlapping proposals for the same beat
    should resolve to the *better* one. Here the higher-scoring clip wins
    and the weaker overlapper is dropped.

    Parameters
    ----------
    threshold : minimum profile-aware total score (0-10) to keep
    max_count : safety valve so a pathological run cannot emit hundreds of
        clips; applied by score (best kept) before the chronological sort
    overlap_ratio : two clips overlapping by more than this fraction of the
        shorter one are treated as the same moment
    profile : Scoring Profile to rank by — when omitted, each clip's own
        ``score_profile`` field is used (ADR-0005)
    """
    if not clips:
        return []

    passing = [c for c in clips if _profile_total(c, profile) >= threshold]
    if not passing:
        return []

    # Strongest first, so overlap resolution and the safety cap both keep
    # the better clip of any competing pair.
    ranked = sorted(
        passing, key=lambda c: _profile_sort_key(c, profile), reverse=True
    )

    kept: list[Clip] = []
    for clip in ranked:
        if len(kept) >= max_count:
            break
        if any(_same_moment(clip, k, overlap_ratio) for k in kept):
            continue
        kept.append(clip)

    kept.sort(key=lambda c: c.start)
    return kept


def _same_moment(a: Clip, b: Clip, overlap_ratio: float) -> bool:
    """True when two clips cover substantially the same beat.

    Measured against the SHORTER clip so a long block that swallows a tight
    moment is recognised as the same beat — using each clip's own duration
    (as ``Clip.overlaps`` does) would miss that case in one direction.
    """
    ov_start = max(a.start, b.start)
    ov_end = min(a.end, b.end)
    if ov_end <= ov_start:
        return False
    shorter = min(a.duration, b.duration)
    if shorter <= 0:
        return False
    return (ov_end - ov_start) / shorter > overlap_ratio


__all__ = ["select_top_clips", "select_above_threshold"]
