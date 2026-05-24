"""
processors/clip_finder/transcript.py — Transcript shaping operations.

Pure functions that transform a list of {start, end, text} segment dicts:
  - merge_short_segments    : tighten neighbouring fragments inside a max
  - condense_for_prompt     : reduce segment count below `max_segments`
  - filter_by_offset        : drop segments that end before `start_offset`
  - extract_discarded       : segments not covered by any selected clip
  - slice_for_clip          : sub-set + re-time relative to a clip range

The transcript is intentionally a `list[dict]` rather than a typed model
because it's the format returned directly by the subtitle parsers and
consumed verbatim by the AI prompt — adding a wrapper here would be a
shallow pass-through.
"""

from __future__ import annotations

from typing import Sequence


Segment = dict      # {"start": float, "end": float, "text": str}


# ─── Merging / condensing ─────────────────────────────────────────────────────

def merge_short_segments(
    segments: Sequence[Segment],
    gap: float = 1.0,
    max_len: int = 200,
) -> list[Segment]:
    """Merge contiguous segments separated by less than `gap` seconds,
    capped at `max_len` characters per merged result."""
    if not segments:
        return list(segments)

    merged = [dict(segments[0])]
    for seg in segments[1:]:
        prev = merged[-1]
        if (seg["start"] - prev["end"]) < gap and len(prev["text"]) < max_len:
            prev["end"] = seg["end"]
            prev["text"] = prev["text"] + " " + seg["text"]
        else:
            merged.append(dict(seg))
    return merged


def condense_for_prompt(
    segments: Sequence[Segment],
    max_segments: int = 500,
) -> list[Segment]:
    """Progressively merge until segment count ≤ max_segments.

    Tries gap thresholds 2 → 4 → 8 → 15 → 30 seconds. Preserves
    timestamps so the LLM can still identify clip boundaries accurately.
    """
    if len(segments) <= max_segments:
        return list(segments)

    merged: list[Segment] = list(segments)
    for gap in (2.0, 4.0, 8.0, 15.0, 30.0):
        merged = [dict(segments[0])]
        for seg in segments[1:]:
            prev = merged[-1]
            if (seg["start"] - prev["end"]) < gap:
                prev["end"] = seg["end"]
                prev["text"] = prev["text"] + " " + seg["text"]
            else:
                merged.append(dict(seg))
        if len(merged) <= max_segments:
            return merged
    return merged


# ─── Range operations ────────────────────────────────────────────────────────

def filter_by_offset(
    segments: Sequence[Segment], start_offset: float
) -> list[Segment]:
    """Drop segments that end before `start_offset` (livestream waiting time)."""
    if start_offset <= 0:
        return list(segments)
    return [seg for seg in segments if seg["end"] > start_offset]


def extract_discarded(
    segments: Sequence[Segment],
    selected_ranges: Sequence[tuple[float, float]],
    padding: float = 5.0,
) -> list[Segment]:
    """Return segments whose midpoint falls outside any selected range.

    `selected_ranges` is a list of (start, end) tuples. Padding extends
    each range by N seconds on both sides before testing coverage.
    """
    if not selected_ranges:
        return list(segments)

    ranges = sorted(
        [(s - padding, e + padding) for s, e in selected_ranges],
        key=lambda r: r[0],
    )
    merged_ranges: list[tuple[float, float]] = [ranges[0]]
    for start, end in ranges[1:]:
        prev_start, prev_end = merged_ranges[-1]
        if start <= prev_end:
            merged_ranges[-1] = (prev_start, max(prev_end, end))
        else:
            merged_ranges.append((start, end))

    discarded: list[Segment] = []
    for seg in segments:
        mid = (seg["start"] + seg["end"]) / 2.0
        if not any(rs <= mid <= re for rs, re in merged_ranges):
            discarded.append(seg)
    return discarded


def slice_for_clip(
    segments: Sequence[Segment],
    clip_start: float,
    clip_end: float,
    padding: float = 0.5,
) -> list[Segment]:
    """Re-time segments overlapping [clip_start, clip_end] to 0-based.

    Used after Phase-2 download to attach a per-clip auto-sub track that
    starts at 0:00 (since the downloaded MP4 is trimmed to the section).
    """
    clip_duration = max(0.0, clip_end - clip_start)
    out: list[Segment] = []
    for seg in segments:
        seg_start = seg["start"]
        seg_end = seg["end"]
        if seg_end < clip_start - padding:
            continue
        if seg_start > clip_end + padding:
            continue
        new_start = max(0.0, seg_start - clip_start)
        new_end = min(clip_duration, seg_end - clip_start)
        if new_end - new_start < 0.1:
            continue
        out.append({
            "start": round(new_start, 3),
            "end": round(new_end, 3),
            "text": seg["text"],
            "source": "autosub",
        })
    return out


__all__ = [
    "Segment",
    "merge_short_segments",
    "condense_for_prompt",
    "filter_by_offset",
    "extract_discarded",
    "slice_for_clip",
]
