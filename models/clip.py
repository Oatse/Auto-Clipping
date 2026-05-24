"""
models/clip.py — Domain model for the Clip Finder feature.

Replaces ad-hoc dict[str, Any] passing across processors / server / UI with a
single typed source of truth. Matches the dataclass style used in
models/transcript.py for consistency.

Key types:
  - HighlightType  : enum for VTuber-style highlight categorisation
  - HunterTag      : enum for single-aspect hunter results
  - SignalKind     : enum for multimodal signal events
  - SignalEvent    : audio peak / silence / chat spike / scene cut
  - ClipScore      : LLM + deterministic feature scores per clip
  - ClipCandidate  : raw clip proposal from a hunter (pre-scoring)
  - Clip           : final scored, refined clip ready for download / display
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


# ─── Enumerations ─────────────────────────────────────────────────────────────


class HighlightType(str, Enum):
    """VTuber-mode highlight category. Mirrors prompt schema in detector."""

    KARMA_ARC = "karma_arc"
    GENUINE_REACTION = "genuine_reaction"
    CLUTCH_PLAY = "clutch_play"
    CHAOTIC_PLEA = "chaotic_plea"
    OTHER = "other"
    UNSPECIFIED = ""

    @classmethod
    def coerce(cls, value: Any) -> "HighlightType":
        if isinstance(value, HighlightType):
            return value
        if not value:
            return cls.UNSPECIFIED
        try:
            return cls(str(value))
        except ValueError:
            return cls.UNSPECIFIED


class HunterTag(str, Enum):
    """Tag emitted by a single-aspect Hunter (Pola A in roadmap)."""

    SCREAM = "scream"
    LAUGHTER = "laughter"
    RAGE = "rage"
    CLUTCH = "clutch"
    FAIL = "fail"
    WHOLESOME = "wholesome"
    META = "meta"
    SCARED = "scared"
    GENERAL = "general"

    @classmethod
    def coerce(cls, value: Any) -> "HunterTag":
        if isinstance(value, HunterTag):
            return value
        if not value:
            return cls.GENERAL
        try:
            return cls(str(value).lower())
        except ValueError:
            return cls.GENERAL


class SignalKind(str, Enum):
    """Type of a multimodal SignalEvent."""

    AUDIO_PEAK = "audio_peak"           # Loud burst (scream / laugh / impact)
    AUDIO_SILENCE = "audio_silence"     # Dead air run > N seconds
    CHAT_SPIKE = "chat_spike"           # Message velocity > baseline x N
    CHAT_EMOTE_STORM = "chat_emote"     # Same emote ≥ K times in window
    CHAT_SUPERCHAT = "chat_superchat"   # Paid superchat moment
    SCENE_CUT = "scene_cut"             # Visual scene change
    GENERIC = "generic"


# ─── Multimodal Signal ────────────────────────────────────────────────────────


@dataclass
class SignalEvent:
    """A timed event extracted from audio / chat / video — fed to the
    clip detector as additional context beyond the raw transcript."""

    kind: SignalKind
    start: float
    end: float
    intensity: float = 0.0        # Normalised 0-1 strength (loudness, msg/sec, etc.)
    label: str = ""                # Human readable hint, e.g. "+18 dB peak"
    sample: str = ""               # Optional sample text (chat msg, transcript line)

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def midpoint(self) -> float:
        return (self.start + self.end) / 2.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "intensity": round(self.intensity, 3),
            "label": self.label,
            "sample": self.sample,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SignalEvent":
        kind_raw = data.get("kind", "generic")
        try:
            kind = SignalKind(kind_raw)
        except ValueError:
            kind = SignalKind.GENERIC
        return cls(
            kind=kind,
            start=float(data.get("start", 0.0)),
            end=float(data.get("end", 0.0)),
            intensity=float(data.get("intensity", 0.0)),
            label=str(data.get("label", "")),
            sample=str(data.get("sample", "")),
        )


# ─── Clip Score ───────────────────────────────────────────────────────────────


@dataclass
class ClipScore:
    """Per-clip score breakdown. All sub-scores in 0-10 range.

    Combines LLM-rated qualitative dimensions with deterministic features
    derived from audio / chat signals so the UI can show a transparent
    "why this clip" rationale.
    """

    retention_hook: float = 0.0     # Hook strength in first 3 seconds (LLM)
    emotional_intensity: float = 0.0 # Peak emotion magnitude (LLM)
    completeness: float = 0.0        # Setup → climax → aftermath structure (LLM)
    replayability: float = 0.0       # Re-watch worthiness (LLM)
    shorts_friendly: float = 0.0     # Vertical / no-context-needed (LLM)
    audio_peak_db: float = 0.0       # Deterministic: max audio peak in dB above baseline
    chat_spike_ratio: float = 0.0    # Deterministic: chat msgs/sec vs baseline
    duration_fit: float = 0.0        # Deterministic: how well duration fits target

    @property
    def total(self) -> float:
        """Weighted overall score, 0-10. Tuned for VTuber clip workflow."""
        weights = {
            "retention_hook": 0.25,
            "emotional_intensity": 0.20,
            "completeness": 0.15,
            "replayability": 0.10,
            "shorts_friendly": 0.10,
            "duration_fit": 0.10,
        }
        # Map deterministic features into 0-10 contributors
        audio_norm = min(10.0, self.audio_peak_db / 3.0)   # +30dB → 10
        chat_norm = min(10.0, self.chat_spike_ratio * 2.0)  # ratio of 5 → 10
        det_total = (audio_norm * 0.05) + (chat_norm * 0.05)

        llm_total = sum(getattr(self, name) * w for name, w in weights.items())
        return round(min(10.0, llm_total + det_total), 2)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["total"] = self.total
        return {k: round(v, 3) if isinstance(v, float) else v for k, v in d.items()}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ClipScore":
        if not data:
            return cls()
        return cls(
            retention_hook=float(data.get("retention_hook", 0.0)),
            emotional_intensity=float(data.get("emotional_intensity", 0.0)),
            completeness=float(data.get("completeness", 0.0)),
            replayability=float(data.get("replayability", 0.0)),
            shorts_friendly=float(data.get("shorts_friendly", 0.0)),
            audio_peak_db=float(data.get("audio_peak_db", 0.0)),
            chat_spike_ratio=float(data.get("chat_spike_ratio", 0.0)),
            duration_fit=float(data.get("duration_fit", 0.0)),
        )


# ─── Clip Candidate (pre-scoring) ─────────────────────────────────────────────


@dataclass
class ClipCandidate:
    """A raw proposal from a Hunter pass — not yet scored or refined."""

    start: float
    end: float
    title: str
    reason: str = ""
    hunter: HunterTag = HunterTag.GENERAL
    highlight_type: HighlightType = HighlightType.UNSPECIFIED
    rescued: bool = False              # True if produced by recheck/rescue pass

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "title": self.title,
            "reason": self.reason,
            "hunter": self.hunter.value,
            "highlight_type": self.highlight_type.value,
            "rescued": self.rescued,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ClipCandidate":
        return cls(
            start=float(data.get("start", 0.0)),
            end=float(data.get("end", 0.0)),
            title=str(data.get("title", "Clip"))[:60],
            reason=str(data.get("reason", ""))[:200],
            hunter=HunterTag.coerce(data.get("hunter", "general")),
            highlight_type=HighlightType.coerce(data.get("highlight_type", "")),
            rescued=bool(data.get("rescued", False)),
        )


# ─── Final Clip ───────────────────────────────────────────────────────────────


@dataclass
class Clip:
    """A finalised clip ready for download, scoring, and UI display."""

    start: float
    end: float
    title: str
    reason: str = ""
    highlight_type: HighlightType = HighlightType.UNSPECIFIED
    hunter: HunterTag = HunterTag.GENERAL
    dead_air_timestamps: list[float] = field(default_factory=list)
    score: ClipScore = field(default_factory=ClipScore)
    rescued: bool = False
    file_idx: int | None = None        # index into job.clip_files when downloaded
    filename: str | None = None         # downloaded mp4 filename
    signals: list[SignalEvent] = field(default_factory=list)  # signals overlapping range

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def is_downloaded(self) -> bool:
        return self.filename is not None and self.file_idx is not None

    def overlaps(self, other: "Clip", min_ratio: float = 0.5) -> bool:
        """True if self overlaps `other` by at least min_ratio of self.duration."""
        ov_start = max(self.start, other.start)
        ov_end = min(self.end, other.end)
        if ov_end <= ov_start:
            return False
        if self.duration <= 0:
            return False
        return (ov_end - ov_start) / self.duration > min_ratio

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "title": self.title,
            "reason": self.reason,
            "highlight_type": self.highlight_type.value,
            "hunter": self.hunter.value,
            "dead_air_timestamps": [round(t, 3) for t in self.dead_air_timestamps],
            "score": self.score.to_dict(),
            "rescued": self.rescued,
        }
        if self.file_idx is not None:
            d["file_idx"] = self.file_idx
        if self.filename:
            d["filename"] = self.filename
        if self.signals:
            d["signals"] = [s.to_dict() for s in self.signals]
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Clip":
        signals_raw = data.get("signals", []) or []
        return cls(
            start=float(data.get("start", 0.0)),
            end=float(data.get("end", 0.0)),
            title=str(data.get("title", "Clip"))[:60],
            reason=str(data.get("reason", ""))[:200],
            highlight_type=HighlightType.coerce(data.get("highlight_type", "")),
            hunter=HunterTag.coerce(data.get("hunter", "general")),
            dead_air_timestamps=[
                float(t) for t in data.get("dead_air_timestamps", [])
                if isinstance(t, (int, float)) or (isinstance(t, str) and _is_numeric(t))
            ],
            score=ClipScore.from_dict(data.get("score")),
            rescued=bool(data.get("rescued", False)),
            file_idx=data.get("file_idx"),
            filename=data.get("filename"),
            signals=[SignalEvent.from_dict(s) for s in signals_raw if isinstance(s, dict)],
        )

    @classmethod
    def from_candidate(
        cls,
        candidate: ClipCandidate,
        score: ClipScore | None = None,
        dead_air: list[float] | None = None,
    ) -> "Clip":
        return cls(
            start=candidate.start,
            end=candidate.end,
            title=candidate.title,
            reason=candidate.reason,
            highlight_type=candidate.highlight_type,
            hunter=candidate.hunter,
            dead_air_timestamps=dead_air or [],
            score=score or ClipScore(),
            rescued=candidate.rescued,
        )


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _is_numeric(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


__all__ = [
    "HighlightType",
    "HunterTag",
    "SignalKind",
    "SignalEvent",
    "ClipScore",
    "ClipCandidate",
    "Clip",
]
