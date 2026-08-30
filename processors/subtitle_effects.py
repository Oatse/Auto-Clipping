from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, assert_never

from models.subtitle_effects import (
    VALID_EFFECT_STRENGTHS,
    VALID_EFFECT_TYPES,
    VALID_WAVE_AXES,
    SegmentEffect,
)


@dataclass(frozen=True, slots=True)
class EffectPreset:
    amplitude: float
    frequency: float


_PRESETS: dict[tuple[str, str], EffectPreset] = {
    ("wave", "soft"): EffectPreset(amplitude=0.0025, frequency=2.0),
    ("wave", "medium"): EffectPreset(amplitude=0.0045, frequency=2.5),
    ("wave", "expert"): EffectPreset(amplitude=0.0075, frequency=3.2),
    ("shake", "soft"): EffectPreset(amplitude=0.0015, frequency=18.0),
    ("shake", "medium"): EffectPreset(amplitude=0.0035, frequency=26.0),
    ("shake", "expert"): EffectPreset(amplitude=0.0065, frequency=36.0),
}


def normalize_segment_effect(
    raw: Mapping[str, str] | None,
) -> SegmentEffect | None:
    if not isinstance(raw, Mapping) or not raw:
        return None

    effect_type = raw.get("type")
    strength = raw.get("strength", "medium")
    if effect_type not in VALID_EFFECT_TYPES or strength not in VALID_EFFECT_STRENGTHS:
        return None

    match effect_type:
        case "wave":
            axis = raw.get("axis")
            if axis not in VALID_WAVE_AXES:
                return None
            return {"type": "wave", "axis": axis, "strength": strength}
        case "shake":
            return {"type": "shake", "strength": strength}
        case unreachable:
            assert_never(unreachable)


def effect_offset(
    effect: Mapping[str, str] | None,
    *,
    local_time: float,
    duration: float,
) -> tuple[float, float]:
    normalized = normalize_segment_effect(effect)
    if normalized is None or local_time < 0.0 or local_time > duration or duration <= 0.0:
        return 0.0, 0.0

    effect_type = normalized["type"]
    strength = normalized["strength"]
    preset = _PRESETS[(effect_type, strength)]

    match effect_type:
        case "wave":
            phase = math.tau * preset.frequency * local_time
            offset = preset.amplitude * math.sin(phase)
            if normalized["axis"] == "horizontal":
                return offset, 0.0
            return 0.0, offset
        case "shake":
            x_phase = math.tau * preset.frequency * local_time
            y_phase = math.tau * (preset.frequency * 0.83) * local_time
            return (
                preset.amplitude * math.sin(x_phase),
                preset.amplitude * math.cos(y_phase),
            )
        case unreachable:
            assert_never(unreachable)


def normalize_segment_position(
    pos_x: float | int | None,
    pos_y: float | int | None,
    override: bool,
) -> tuple[float | None, float | None, bool]:
    if not override or not isinstance(pos_x, (int, float)) or not isinstance(pos_y, (int, float)):
        return None, None, False
    return (
        max(0.0, min(100.0, float(pos_x))),
        max(0.0, min(100.0, float(pos_y))),
        True,
    )
