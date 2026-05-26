"""
processors/clip_finder/visual_signals.py — Scene-cut event extraction.

Detects visual scene transitions in the source video using FFmpeg's
``select=gt(scene,N)`` filter and emits them as ``SignalEvent`` of kind
``SCENE_CUT``. Scene cuts feed two consumers:

  - ``processors.clip_finder.scoring`` — already aware of ``signals``;
    a clip with a scene cut at its boundary scores cleaner than one
    that ends mid-shot.
  - ``processors.short_maker`` reframe — when a scene cut sits inside
    the Clip, the smart-static crop gets re-computed for each
    sub-segment so the subject stays in frame after the cut.

Why a separate module from ``audio_signals.py``: the input is the
already-downloaded source video (we own it on disk in the All In
Workspace per ADR-0002 Q12), not a yt-dlp re-fetch. The extraction is
pure FFmpeg, no Python deps.

Public API:
    extract(*, video_path, ...) -> list[SignalEvent]
    segments_between_cuts(cuts, total_duration) -> list[tuple[start, end]]
"""

from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path
from typing import Callable, Sequence

from models.clip import SignalEvent, SignalKind

LogFn = Callable[[str], None]


# ─── Configuration ───────────────────────────────────────────────────────────

# scene threshold passed to ffmpeg ``select=gt(scene,N)``.
# 0.4 is the FFmpeg recommended default — lower = more sensitive (more
# false positives), higher = misses subtle cuts.
SCENE_THRESHOLD = 0.4

# Cooldown between two cuts. FFmpeg sometimes flags a cluster of frames
# as cut events for fades or fast camera moves; merge anything closer
# than this so reframe doesn't re-compute crops on noise.
CUT_MIN_GAP_SECONDS = 0.5


# ─── Public API ──────────────────────────────────────────────────────────────


async def extract(
    *,
    video_path: Path,
    ffmpeg_path: str = "ffmpeg",
    threshold: float = SCENE_THRESHOLD,
    log_fn: LogFn | None = None,
) -> list[SignalEvent]:
    """Return scene-cut SignalEvents for ``video_path``.

    Each cut is a *boundary* (an instant), not a span. We model it as a
    zero-duration ``SignalEvent`` with ``start == end`` so downstream
    consumers can decide whether to treat it as a point or pad it into
    a small range. ``intensity`` is the FFmpeg scene score (0..1) which
    correlates with how visually different the next frame is.

    Returns an empty list (not None) on:
      - missing FFmpeg,
      - missing or unreadable input,
      - parse failure on FFmpeg output.

    Failure is silent because scene cuts are an *enrichment* signal —
    the rest of the pipeline degrades gracefully without them.
    """
    if not shutil.which(ffmpeg_path):
        if log_fn:
            log_fn("VisualSignals: ffmpeg not on PATH, skipping scene-cut extraction")
        return []
    if not video_path.exists():
        if log_fn:
            log_fn(f"VisualSignals: input missing: {video_path}")
        return []

    if log_fn:
        log_fn(f"VisualSignals: scanning {video_path.name} for scene cuts...")

    cmd = [
        ffmpeg_path,
        "-hide_banner",
        "-nostats",
        "-i", str(video_path),
        "-vf", f"select='gt(scene,{threshold})',showinfo",
        "-f", "null",
        "-",
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    text = (stderr or b"").decode("utf-8", errors="replace")

    cuts = _parse_showinfo(text)
    cuts = _merge_close(cuts, min_gap=CUT_MIN_GAP_SECONDS)

    events = [
        SignalEvent(
            kind=SignalKind.SCENE_CUT,
            start=round(t, 2),
            end=round(t, 2),
            intensity=round(score, 3),
            label=f"scene cut (Δ={score:.2f})",
        )
        for t, score in cuts
    ]
    if log_fn:
        log_fn(f"VisualSignals: {len(events)} scene cut(s) detected")
    return events


def segments_between_cuts(
    cuts: Sequence[SignalEvent],
    total_duration: float,
) -> list[tuple[float, float]]:
    """Convert scene-cut events into the contiguous segments they bound.

    Useful for the reframe stage — call once per Clip with the cuts that
    fall inside the Clip's range, and you get back the list of shot
    segments to compute a per-segment crop on.

    Example: cuts at [t=2.0, t=5.0] in a 10s clip →
        [(0.0, 2.0), (2.0, 5.0), (5.0, 10.0)]

    Returns the full ``[(0.0, total_duration)]`` when no cuts are
    supplied, so callers can always iterate over at least one segment.
    """
    if total_duration <= 0:
        return []
    cut_times = sorted({
        round(c.start, 3) for c in cuts
        if c.kind == SignalKind.SCENE_CUT
        and 0.0 < c.start < total_duration
    })
    if not cut_times:
        return [(0.0, total_duration)]

    segments: list[tuple[float, float]] = []
    cursor = 0.0
    for t in cut_times:
        if t > cursor:
            segments.append((cursor, t))
        cursor = t
    if cursor < total_duration:
        segments.append((cursor, total_duration))
    return segments


# ─── Parsers ─────────────────────────────────────────────────────────────────


_PTS_RE = re.compile(r"pts_time:([\d.]+)")
_SCORE_RE = re.compile(r"scene_score=([\d.]+)")


def _parse_showinfo(text: str) -> list[tuple[float, float]]:
    """Pull ``(timestamp, score)`` pairs out of FFmpeg's showinfo output.

    Each scene-cut frame produces a ``[Parsed_showinfo`` line with a
    ``pts_time:`` timestamp; the matching ``[Parsed_select`` line above
    it carries the ``scene_score=`` value. We pair them up by order of
    appearance — both filters emit one line per matching frame, so the
    indices line up.
    """
    pts_values: list[float] = []
    scores: list[float] = []
    for line in text.splitlines():
        if "Parsed_showinfo" in line:
            m = _PTS_RE.search(line)
            if m:
                try:
                    pts_values.append(float(m.group(1)))
                except ValueError:
                    pass
            continue
        if "Parsed_select" in line:
            m = _SCORE_RE.search(line)
            if m:
                try:
                    scores.append(float(m.group(1)))
                except ValueError:
                    pass

    if not pts_values:
        return []
    if len(scores) < len(pts_values):
        # Pad missing scores with the threshold so we still emit the cut.
        scores.extend([SCENE_THRESHOLD] * (len(pts_values) - len(scores)))
    return list(zip(pts_values, scores))


def _merge_close(
    cuts: list[tuple[float, float]],
    *,
    min_gap: float,
) -> list[tuple[float, float]]:
    """Drop cuts that come too close to the previous one.

    Keeps the *first* cut in a cluster — that's the one that actually
    marks the visual boundary; the trailing frames are usually fade
    artefacts.
    """
    if not cuts:
        return []
    merged: list[tuple[float, float]] = [cuts[0]]
    for t, score in cuts[1:]:
        if t - merged[-1][0] >= min_gap:
            merged.append((t, score))
    return merged


__all__ = ["extract", "segments_between_cuts", "SCENE_THRESHOLD"]
