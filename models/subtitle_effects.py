"""Typed values used by per-segment subtitle text effects."""

from __future__ import annotations

from typing import Final, Literal, TypedDict

EffectType = Literal["wave", "shake"]
WaveAxis = Literal["horizontal", "vertical"]
EffectStrength = Literal["soft", "medium", "expert"]


class SegmentEffect(TypedDict, total=False):
    """JSON-compatible effect configuration stored on a transcript segment."""

    type: EffectType
    axis: WaveAxis
    strength: EffectStrength


VALID_EFFECT_TYPES: Final[frozenset[str]] = frozenset({"wave", "shake"})
VALID_WAVE_AXES: Final[frozenset[str]] = frozenset({"horizontal", "vertical"})
VALID_EFFECT_STRENGTHS: Final[frozenset[str]] = frozenset(
    {"soft", "medium", "expert"},
)
