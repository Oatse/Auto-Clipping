"""
processors/clip_finder/heuristics.py — User-instruction parsing helpers.

Pure functions, no I/O. The interface promises:
  - given user instructions, return (min_clip_seconds, max_clip_seconds)
  - given user instructions, return whether VTuber-mode is implied
  - format seconds as H:MM:SS / M:SS / "Xm Ys"
  - convert "1:22" / "1:02:30" / 82 / "82.5" to total seconds
"""

from __future__ import annotations

import re


# ─── Duration hint parsing ────────────────────────────────────────────────────

_RANGE_MIN_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*[-–~]\s*(\d+(?:[.,]\d+)?)\s*"
    r"(?:menit|minutes?|mins?)",
    re.IGNORECASE,
)
_RANGE_SEC_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*[-–~]\s*(\d+(?:[.,]\d+)?)\s*"
    r"(?:detik|seconds?|secs?|s\b)",
    re.IGNORECASE,
)
_SINGLE_MIN_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(?:menit|minutes?|mins?)", re.IGNORECASE
)
_SINGLE_SEC_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(?:detik|seconds?|secs?|s\b)", re.IGNORECASE
)


def parse_duration_hints(
    instructions: str, video_duration: float
) -> tuple[float, float]:
    """Parse min/max clip duration from user instructions.

    Recognises Indonesian + English duration phrases such as
    "2-3 menit", "30-60 seconds", "60s clips". Falls back to adaptive
    defaults based on video length.
    """
    if not instructions:
        return _adaptive_defaults(video_duration)

    text = instructions.lower()

    m = _RANGE_MIN_RE.search(text)
    if m:
        lo = float(m.group(1).replace(",", "."))
        hi = float(m.group(2).replace(",", "."))
        return lo * 60, hi * 60

    m = _RANGE_SEC_RE.search(text)
    if m:
        lo = float(m.group(1).replace(",", "."))
        hi = float(m.group(2).replace(",", "."))
        return lo, hi

    m = _SINGLE_MIN_RE.search(text)
    if m:
        target = float(m.group(1).replace(",", ".")) * 60
        return max(10, target * 0.5), target * 1.5

    m = _SINGLE_SEC_RE.search(text)
    if m:
        target = float(m.group(1).replace(",", "."))
        return max(5, target * 0.5), target * 1.5

    return _adaptive_defaults(video_duration)


def _adaptive_defaults(video_duration: float) -> tuple[float, float]:
    if video_duration > 3600:
        return 30.0, 300.0
    if video_duration > 600:
        return 15.0, 180.0
    return 10.0, 120.0


# ─── VTuber mode detection ────────────────────────────────────────────────────

_VTUBER_KEYWORDS = (
    "vtuber",
    "highlight_type",
    "dead_air",
    "karma arc",
    "karma_arc",
    "chaotic plea",
    "genuine reaction",
    "full cycle",
    "clutch play",
    "peak moment",
    "scream",
    "highlight",
    "stream highlight",
)


def is_vtuber_mode(instructions: str) -> bool:
    """True if instructions request VTuber-style structured output.

    Triggered by keywords from the VTuber Highlights preset or related
    streamer/highlight terminology. When True, the prompt schema gains
    `highlight_type` and `dead_air_timestamps` fields.
    """
    if not instructions:
        return False
    text = instructions.lower()
    return any(kw in text for kw in _VTUBER_KEYWORDS)


# ─── Time formatting / parsing ────────────────────────────────────────────────


def fmt_time(secs: float) -> str:
    """Format seconds as H:MM:SS or M:SS for user display."""
    h = int(secs // 3600)
    m = int((secs % 3600) // 60)
    s = int(secs % 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def fmt_duration(secs: float) -> str:
    """Format as 'Xm Ys' or 'Ys' for clip-length display."""
    m = int(secs // 60)
    s = int(secs % 60)
    if m > 0:
        return f"{m}m {s}s"
    return f"{s}s"


def to_seconds(value) -> float:
    """Convert int/float/'82.5'/'1:22'/'1:02:30' → total seconds."""
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
    "parse_duration_hints",
    "is_vtuber_mode",
    "fmt_time",
    "fmt_duration",
    "to_seconds",
]
