"""
processors/clip_finder/selection.py — Top-N selection with diversification.

After scoring, we usually have more candidates than the user actually
wants. This module picks the best subset using two constraints:

  1. Total score (descending)
  2. Diversity:
       - Different hunter tags (avoid five "scream" clips in a row)
       - Spread across the timeline (avoid all clips from minute 30-35)
       - Optional total-duration budget for compilation use-case
"""

from __future__ import annotations

from typing import Sequence

from models.clip import Clip, HunterTag


def select_top_clips(
    clips: Sequence[Clip],
    *,
    max_count: int = 12,
    duration_budget: float | None = None,
    diversify_tags: bool = True,
    timeline_buckets: int = 6,
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
    """
    if not clips:
        return []

    sorted_clips = sorted(clips, key=lambda c: -c.score.total)
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


__all__ = ["select_top_clips"]
