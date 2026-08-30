"""
models/pov_group.py — Domain model for the Multi POV workspace (Workspace 05).

Introduces two types that do not exist in the Clip Finder domain:

  POVPerspective  — One Moment from one POV Source inside a POV Group.
                    Carries source identity, time range, and LLM-derived
                    clip metadata so the UI can render each perspective
                    card independently.

  POVGroup        — A real-world event (clutch, fail, reaction) that was
                    independently captured by N ≥ 1 POV Sources. This is
                    the first-class unit of the Multi POV workspace — not
                    individual Clips.

Design decisions (see multi_pov_planning.md):
  - POVGroup is an Opaque A (Grill #2): it owns Perspectives, not the
    other way around.  Keeping the event as the top-level entity lets the
    UI group-first, and lets scoring represent the event as a whole.
  - is_multi_pov = True  when confidence ≥ CONFIDENCE_THRESHOLD AND
    len(perspectives) ≥ 2. Groups that fall below the threshold are kept
    as single-source entries (Grill #4 — graceful degradation).
  - No circular imports: this module only imports from models.clip, which
    has no dependency on any processor.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from models.clip import ClipScore, HunterTag


# ─── Constants ────────────────────────────────────────────────────────────────

# Minimum confidence (0.0–1.0) from the cross-matching LLM for a group
# to be considered a true multi-POV moment (Grill #4). Groups below this
# threshold are kept but flagged is_multi_pov=False and rendered in the
# "Single-Source Moments" section instead of the main grid.
CONFIDENCE_THRESHOLD: float = 0.7


# ─── POVPerspective ───────────────────────────────────────────────────────────


@dataclass
class POVPerspective:
    """One Moment from one POV Source inside a POV Group.

    ``source_idx`` is the index into ``MultiPOVJob.sources``; it is the
    stable cross-reference between perspectives and the originating URL /
    label.  ``file_path`` and ``autosub_path`` are populated during Phase 2
    (download) and are ``None`` beforehand.
    """

    source_idx: int
    url: str
    label: str                          # "Player A", "Caster", or video_title fallback
    video_title: str | None
    start: float
    end: float
    title: str                          # Moment title from per-source Clip Finder
    reason: str                         # Why this moment was selected
    score: ClipScore = field(default_factory=ClipScore)
    hunter: HunterTag = HunterTag.GENERAL
    file_path: str | None = None        # Absolute path to MP4 (set after download)
    autosub_path: str | None = None     # Absolute path to *_autosub.json

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def is_downloaded(self) -> bool:
        return self.file_path is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_idx": self.source_idx,
            "url": self.url,
            "label": self.label,
            "video_title": self.video_title,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "duration": round(self.duration, 3),
            "title": self.title,
            "reason": self.reason,
            "score": self.score.to_dict(),
            "hunter": self.hunter.value,
            "file_path": self.file_path,
            "autosub_path": self.autosub_path,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "POVPerspective":
        from models.clip import ClipScore, HunterTag
        return cls(
            source_idx=int(data.get("source_idx", 0)),
            url=str(data.get("url", "")),
            label=str(data.get("label", "")),
            video_title=data.get("video_title"),
            start=float(data.get("start", 0.0)),
            end=float(data.get("end", 0.0)),
            title=str(data.get("title", "Clip")),
            reason=str(data.get("reason", "")),
            score=ClipScore.from_dict(data.get("score")),
            hunter=HunterTag.coerce(data.get("hunter", "general")),
            file_path=data.get("file_path"),
            autosub_path=data.get("autosub_path"),
        )


# ─── POVGroup ─────────────────────────────────────────────────────────────────


@dataclass
class POVGroup:
    """A real-world event captured from ≥1 POV Sources.

    ``group_id`` is a URL-safe slug derived from ``title`` at creation time
    and used as the on-disk folder name (``groups/{group_id}/``).

    ``confidence`` is the 0.0–1.0 value returned by the cross-matching LLM.
    ``is_multi_pov`` is True when confidence ≥ CONFIDENCE_THRESHOLD AND
    len(perspectives) ≥ 2 — computed at construction.

    ``perspectives`` are ordered by source_idx so that the UI always shows
    sources in the order the user entered them.
    """

    group_id: str                       # slug, e.g. "clutch-round-5"
    title: str                          # LLM-generated event title
    reason: str                         # Why these perspectives share an event
    confidence: float                   # 0.0–1.0 from cross-matching LLM
    perspectives: list[POVPerspective] = field(default_factory=list)
    is_multi_pov: bool = False          # True if confidence ≥ threshold AND ≥2 POVs

    # ── Factory ───────────────────────────────────────────────────────────

    @classmethod
    def create(
        cls,
        *,
        title: str,
        reason: str,
        confidence: float,
        perspectives: list[POVPerspective],
    ) -> "POVGroup":
        """Construct a POVGroup, computing group_id and is_multi_pov automatically."""
        group_id = _slugify(title)
        is_multi = confidence >= CONFIDENCE_THRESHOLD and len(perspectives) >= 2
        return cls(
            group_id=group_id,
            title=title,
            reason=reason,
            confidence=confidence,
            perspectives=sorted(perspectives, key=lambda p: p.source_idx),
            is_multi_pov=is_multi,
        )

    # ── Properties ────────────────────────────────────────────────────────

    @property
    def perspective_count(self) -> int:
        return len(self.perspectives)

    @property
    def is_downloaded(self) -> bool:
        """True when every perspective has been downloaded."""
        return all(p.is_downloaded for p in self.perspectives)

    # ── Serialisation ─────────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "title": self.title,
            "reason": self.reason,
            "confidence": round(self.confidence, 3),
            "is_multi_pov": self.is_multi_pov,
            "perspective_count": self.perspective_count,
            "perspectives": [p.to_dict() for p in self.perspectives],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "POVGroup":
        perspectives = [
            POVPerspective.from_dict(p)
            for p in data.get("perspectives", [])
            if isinstance(p, dict)
        ]
        return cls(
            group_id=str(data.get("group_id", "")),
            title=str(data.get("title", "")),
            reason=str(data.get("reason", "")),
            confidence=float(data.get("confidence", 0.0)),
            perspectives=perspectives,
            is_multi_pov=bool(data.get("is_multi_pov", False)),
        )


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _slugify(text: str, max_length: int = 40) -> str:
    """Convert a title to a URL-safe folder-name slug.

    Example: "Jett Clutch 1v3 Round 5!" → "jett-clutch-1v3-round-5"
    """
    slug = text.lower()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = slug.strip("-")
    return slug[:max_length]


__all__ = [
    "CONFIDENCE_THRESHOLD",
    "POVPerspective",
    "POVGroup",
]
