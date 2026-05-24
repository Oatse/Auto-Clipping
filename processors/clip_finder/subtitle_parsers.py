"""
processors/clip_finder/subtitle_parsers.py — Pure subtitle file parsers.

Three formats are supported (the ones yt-dlp can actually deliver for
auto-subs and manual subs): json3, srt, vtt.

Each parser returns a `list[Segment]` shaped as
`{"start": float_seconds, "end": float_seconds, "text": str}`.

Adjacent fragments are merged via `transcript.merge_short_segments` so
the output is suitable for direct prompting.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .transcript import Segment, merge_short_segments


# ─── Public parsers ──────────────────────────────────────────────────────────

def parse_json3(path: Path) -> list[Segment]:
    """Parse YouTube's json3 auto-sub format."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    segments: list[Segment] = []
    for ev in data.get("events", []):
        start_ms = ev.get("tStartMs", 0)
        dur_ms = ev.get("dDurationMs", 0)
        segs = ev.get("segs", [])
        text = "".join(s.get("utf8", "") for s in segs).strip()
        text = re.sub(r"\n", " ", text)
        if text and text != "\n":
            segments.append({
                "start": start_ms / 1000.0,
                "end": (start_ms + dur_ms) / 1000.0,
                "text": text,
            })

    return merge_short_segments(segments)


def parse_srt(path: Path) -> list[Segment]:
    content = path.read_text(encoding="utf-8", errors="replace")
    return merge_short_segments(_parse_timed_text(content))


def parse_vtt(path: Path) -> list[Segment]:
    content = path.read_text(encoding="utf-8", errors="replace")
    return merge_short_segments(_parse_timed_text(content))


# ─── Internal SRT/VTT timed-text parser ──────────────────────────────────────

_TS_HMS_RE = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*"
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{3})"
)
_TS_MS_RE = re.compile(
    r"(\d{1,2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{1,2}):(\d{2})[,.](\d{3})"
)
_TAG_RE = re.compile(r"<[^>]+>")


def _parse_timed_text(content: str) -> list[Segment]:
    segments: list[Segment] = []
    for block in re.split(r"\n\n+", content.strip()):
        ts_line: str | None = None
        text_lines: list[str] = []
        for line in block.strip().split("\n"):
            if "-->" in line:
                ts_line = line
            elif ts_line is not None:
                text_lines.append(line)

        if not ts_line or not text_lines:
            continue

        m = _TS_HMS_RE.search(ts_line)
        if m:
            g = m.groups()
            start = int(g[0]) * 3600 + int(g[1]) * 60 + int(g[2]) + int(g[3]) / 1000
            end = int(g[4]) * 3600 + int(g[5]) * 60 + int(g[6]) + int(g[7]) / 1000
        else:
            m = _TS_MS_RE.search(ts_line)
            if not m:
                continue
            g = m.groups()
            start = int(g[0]) * 60 + int(g[1]) + int(g[2]) / 1000
            end = int(g[3]) * 60 + int(g[4]) + int(g[5]) / 1000

        text = " ".join(text_lines).strip()
        text = _TAG_RE.sub("", text)
        if text:
            segments.append({"start": start, "end": end, "text": text})

    return segments


__all__ = ["parse_json3", "parse_srt", "parse_vtt"]
