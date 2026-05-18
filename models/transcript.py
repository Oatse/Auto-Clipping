"""
models/transcript.py — Data contracts for the pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from loguru import logger


# ─── Phase 1 Output ───────────────────────────────────────────────────────────

@dataclass
class WordTimestamp:
    """A single word with its precise start/end timestamps from the STT engine."""
    word: str
    start: float
    end: float
    score: float = 0.0  # Alignment confidence score
    source: str = ""    # Provenance: "elevenlabs" | "interpolated" | ""

    @property
    def duration(self) -> float:
        return self.end - self.start

    def to_dict(self) -> dict[str, Any]:
        d = {
            "word": self.word,
            "start": self.start,
            "end": self.end,
            "score": self.score,
        }
        if self.source:
            d["source"] = self.source
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WordTimestamp":
        return cls(
            word=data["word"],
            start=data["start"],
            end=data["end"],
            score=data.get("score", 0.0),
            source=data.get("source", ""),
        )


@dataclass
class TranscriptSegment:
    """
    A single speech segment from the ElevenLabs STT pipeline.
    Retains start/end/text/speaker through all pipeline phases.
    """
    start: float
    end: float
    text: str
    speaker: str
    words: list[WordTimestamp] = field(default_factory=list)
    pos_x: float | None = None        # Per-segment X position override (0-100%)
    pos_y: float | None = None        # Per-segment Y position override (0-100%)
    pos_override: bool = False         # Whether to use custom position

    @property
    def duration(self) -> float:
        return self.end - self.start

    def to_dict(self) -> dict[str, Any]:
        d = {
            "start": self.start,
            "end": self.end,
            "text": self.text,
            "speaker": self.speaker,
            "words": [w.to_dict() for w in self.words],
        }
        if self.pos_override:
            d["pos_x"] = self.pos_x
            d["pos_y"] = self.pos_y
            d["pos_override"] = True
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TranscriptSegment":
        return cls(
            start=data["start"],
            end=data["end"],
            text=data["text"],
            speaker=data.get("speaker", "SPEAKER_00"),
            words=[WordTimestamp.from_dict(w) for w in data.get("words", [])],
            pos_x=data.get("pos_x"),
            pos_y=data.get("pos_y"),
            pos_override=data.get("pos_override", False),
        )


# ─── Phase 3 Output ───────────────────────────────────────────────────────────

@dataclass
class PycapsWordEntry:
    """
    A single word entry in the Pycaps-compatible subtitle JSON.
    Uses global video timestamps (not relative to segment).
    """
    word: str
    global_start: float     # Segment_Start_In_Video + Word_Start
    global_end: float       # Segment_Start_In_Video + Word_End

    def to_dict(self) -> dict[str, Any]:
        return {
            "word": self.word,
            "start": self.global_start,
            "end": self.global_end,
        }


# ─── Timestamp Sanitisation ──────────────────────────────────────────────────
#
# The implementation now lives in ``processors.timing``.  This module keeps a
# thin compatibility shim so legacy callers (``from models.transcript import
# sanitize_timestamps``) keep working without change.  New code should import
# ``Sanitizer`` / ``TimingPolicy`` from ``processors.timing`` directly.
#
# The processors-package import is **lazy** (done inside the function body)
# so that loading this module does NOT trigger ``processors/__init__.py``.
# That __init__ imports ``processors.elevenlabs_stt`` which imports back into
# this module — a hard circular import at module level.


def _estimate_max_word_duration(word_text: str) -> float:
    """Legacy alias — see ``processors.timing.sanitizer.estimate_max_word_duration``."""
    from processors.timing.sanitizer import (
        estimate_max_word_duration as _impl,
    )
    from processors.timing.policy import TimingPolicy
    return _impl(word_text, TimingPolicy())


def sanitize_timestamps(
    segments: list[TranscriptSegment],
    silence_cap: float | None = None,
    segment_level_only: bool = False,
) -> list[TranscriptSegment]:
    """Compatibility shim around ``processors.timing.Sanitizer``.

    Behaviour is unchanged from the previous in-module implementation:
    the sanitizer is **speaker-aware** (cross-speaker interruption is
    preserved), word-duration cap auto-adapts to global speech rate,
    and ``segment_level_only=True`` skips word-level passes for segments
    whose word timestamps were artificially redistributed.

    Parameters
    ----------
    segments:
        Transcript segments to sanitize (modified **in-place**).
    silence_cap:
        Override for ``TimingPolicy.silence_cap``.  ``None`` keeps the
        default (2.0 s).
    segment_level_only:
        When True, skip word-level sanitization (passes 1–3).  Only fix
        segment ordering and same-speaker overlap.  Default False.

    Returns
    -------
    list[TranscriptSegment]
        The same list, sanitized.
    """
    # Lazy import — see the comment above about the circular import.
    from processors.timing.policy import TimingPolicy
    from processors.timing.sanitizer import Sanitizer

    if silence_cap is None:
        policy = TimingPolicy()
    else:
        from dataclasses import replace as _dc_replace
        policy = _dc_replace(TimingPolicy(), silence_cap=silence_cap)

    sanitizer = Sanitizer(policy)
    if segment_level_only:
        return sanitizer.sanitize_segment_only(segments)
    return sanitizer.sanitize(segments)
