"""
processors/clip_finder/clip_selection.py — Validation, salvage, dedup of LLM-returned clips.

The Gemini API can:
  - return JSON wrapped in prose
  - return truncated JSON (hit maxOutputTokens mid-array)
  - return clip durations outside the user's requested range

This module owns the entire defensive-parsing surface so the detector
can stay focused on prompt orchestration.
"""

from __future__ import annotations

import json
import re
from typing import Iterable

from loguru import logger

from models.clip import (
    Clip,
    ClipCandidate,
    HighlightType,
    HunterTag,
)


class ClipFinderError(RuntimeError):
    """Raised when a clip finder operation fails irrecoverably."""


# ─── Top-level entry ─────────────────────────────────────────────────────────


def parse_candidates_json(
    text: str,
    *,
    min_duration: float,
    max_duration: float,
    hunter: HunterTag = HunterTag.GENERAL,
    rescued: bool = False,
) -> list[ClipCandidate]:
    """Parse Gemini output → validated list[ClipCandidate]."""
    raw_objects = _extract_objects(text)
    if not raw_objects:
        return []

    candidates: list[ClipCandidate] = []
    for obj in raw_objects:
        if not isinstance(obj, dict):
            continue
        if not all(k in obj for k in ("start", "end", "title")):
            continue
        try:
            start = _to_seconds(obj["start"])
            end = _to_seconds(obj["end"])
        except (ValueError, TypeError):
            continue
        if end <= start or (end - start) < 1.0:
            continue

        cand = ClipCandidate(
            start=start,
            end=end,
            title=str(obj.get("title", "Clip"))[:60],
            reason=str(obj.get("reason", ""))[:200],
            hunter=HunterTag.coerce(obj.get("hunter", hunter.value)),
            highlight_type=HighlightType.coerce(obj.get("highlight_type", "")),
            rescued=rescued,
        )
        candidates.append(cand)

    return _enforce_duration(candidates, min_duration, max_duration)


# ─── JSON extraction (handles wrapping prose + truncation) ───────────────────


def _extract_objects(text: str) -> list[dict]:
    """Try increasingly tolerant strategies to extract JSON objects."""
    # 1. Pure JSON array
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return [data]
    except json.JSONDecodeError:
        pass

    # 2. JSON array embedded in prose
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group())
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            pass

    # 3. Salvage individual {...} objects from truncated output
    salvaged = _salvage_truncated(text)
    if salvaged:
        logger.warning(
            "Salvaged {} complete clip(s) from truncated LLM response",
            len(salvaged),
        )
    return salvaged


def _salvage_truncated(text: str) -> list[dict]:
    """Find every complete {...} JSON object in `text`."""
    objects: list[dict] = []
    depth = 0
    start_idx: int | None = None
    in_str = False
    escape = False

    for i, ch in enumerate(text):
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            if depth == 0:
                start_idx = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start_idx is not None:
                    blob = text[start_idx : i + 1]
                    try:
                        obj = json.loads(blob)
                        if isinstance(obj, dict):
                            objects.append(obj)
                    except json.JSONDecodeError:
                        pass
                    start_idx = None
    return objects


# ─── Duration enforcement & dedup ────────────────────────────────────────────


def _enforce_duration(
    candidates: list[ClipCandidate],
    min_duration: float,
    max_duration: float,
) -> list[ClipCandidate]:
    """Extend short clips tail-first; cap long clips from the end side.

    For VTuber / JP-translated clips the hook lives in the first 0-3 s of
    the candidate range. The pre-May-28 behaviour expanded a too-short
    clip symmetrically around the centre, which pushed the hook into the
    middle of the Moment and weakened retention. We now bias expansion
    toward the tail (preserves hook) and only borrow from the head when
    the tail can't supply enough length. See May-28 audit "#12".
    """
    out: list[ClipCandidate] = []
    for c in candidates:
        dur = c.duration
        start, end = c.start, c.end
        if dur < min_duration:
            needed = min_duration - dur
            # Tail first — keeps the hook anchored at the original start.
            end = end + needed
            # Tail expansion is unbounded here because we don't know
            # the source-video duration in this module. Downstream
            # download stage clamps to actual video length.
        elif dur > max_duration:
            # Cap from the end side — the punchline / payoff is usually
            # earlier in a long Gemini-overshoot range, so trimming the
            # tail loses less than trimming the head would.
            end = start + max_duration
        out.append(
            ClipCandidate(
                start=round(start, 3),
                end=round(end, 3),
                title=c.title,
                reason=c.reason,
                hunter=c.hunter,
                highlight_type=c.highlight_type,
                rescued=c.rescued,
            )
        )
    return out


def deduplicate_candidates(
    candidates: Iterable[ClipCandidate],
    overlap_ratio: float = 0.5,
) -> list[ClipCandidate]:
    """Remove overlapping / duplicate-titled candidates. Keeps earliest."""
    sorted_cands = sorted(candidates, key=lambda c: c.start)
    deduped: list[ClipCandidate] = []
    seen_titles: set[str] = set()

    for c in sorted_cands:
        title_key = c.title.strip().lower()
        if title_key in seen_titles:
            continue

        is_overlap = False
        for accepted in deduped:
            ov_start = max(c.start, accepted.start)
            ov_end = min(c.end, accepted.end)
            if ov_end > ov_start:
                ov_dur = ov_end - ov_start
                if c.duration > 0 and (ov_dur / c.duration) > overlap_ratio:
                    is_overlap = True
                    break
        if is_overlap:
            continue
        deduped.append(c)
        seen_titles.add(title_key)

    return deduped


def deduplicate_clips(clips: Iterable[Clip], overlap_ratio: float = 0.5) -> list[Clip]:
    """Same as deduplicate_candidates but operates on Clip instances."""
    sorted_clips = sorted(clips, key=lambda c: c.start)
    deduped: list[Clip] = []
    seen_titles: set[str] = set()

    for c in sorted_clips:
        title_key = c.title.strip().lower()
        if title_key in seen_titles:
            continue
        is_overlap = False
        for accepted in deduped:
            if c.overlaps(accepted, overlap_ratio):
                is_overlap = True
                break
        if is_overlap:
            continue
        deduped.append(c)
        seen_titles.add(title_key)
    return deduped


# ─── helpers ─────────────────────────────────────────────────────────────────


def _to_seconds(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    try:
        return float(s)
    except ValueError:
        pass
    parts = s.split(":")
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    if len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    raise ValueError(f"Cannot convert {value!r} to seconds")


__all__ = [
    "ClipFinderError",
    "parse_candidates_json",
    "deduplicate_candidates",
    "deduplicate_clips",
]
