"""
web.services.all_in.presets — Named subtitle style presets.

The All In Workspace skips the manual editor screen, so users pick
one of three named presets instead of building a ``style_config``
from scratch.  At render time the chosen preset's dict is fed into
``web.services.pipeline_runner.run_render_pipeline`` exactly as if
it had come from the editor UI.

Adding a preset is a one-place change: add an entry to ``PRESETS``,
add the enum member to ``models.CaptionPreset``, surface it in the
template's segmented control.

The dict shape mirrors what the existing render pipeline already
accepts — see ``processors/subtitle_renderer.py`` for the consumer.
"""

from __future__ import annotations

from typing import Any

from .models import CaptionPreset


# ─── Preset definitions ───────────────────────────────────────────────────────

# Bold — large white text + black stroke, centred bottom-third.
# The default for short-form social content where caption legibility on
# small mobile screens matters more than visual restraint.
_BOLD: dict[str, Any] = {
    "font_family": "Inter",
    "font_weight": 800,
    "font_size_pct": 6.0,            # % of video height
    "fill_color": "#FFFFFF",
    "stroke_color": "#000000",
    "stroke_width_px": 6,
    "shadow_blur_px": 0,
    "position": "bottom-third",      # vertical placement
    "alignment": "center",           # horizontal alignment
    "max_words_per_line": 4,
    "uppercase": True,
    "active_word_highlight": False,
}

# Minimal — small white text, no stroke, lower-third.  For documentary
# / narrative content where the captions support the visuals rather
# than dominate them.
_MINIMAL: dict[str, Any] = {
    "font_family": "Inter",
    "font_weight": 500,
    "font_size_pct": 3.5,
    "fill_color": "#FFFFFF",
    "stroke_color": "#000000",
    "stroke_width_px": 2,
    "shadow_blur_px": 4,
    "position": "lower-third",
    "alignment": "center",
    "max_words_per_line": 6,
    "uppercase": False,
    "active_word_highlight": False,
}

# Karaoke — word-by-word highlight on the active word.  The
# subtitle_renderer reads ``active_word_highlight`` and paints the
# current word in ``highlight_color`` while the rest of the line
# uses ``fill_color``.
_KARAOKE: dict[str, Any] = {
    "font_family": "Inter",
    "font_weight": 700,
    "font_size_pct": 5.0,
    "fill_color": "#FFFFFF",
    "stroke_color": "#000000",
    "stroke_width_px": 4,
    "shadow_blur_px": 0,
    "position": "bottom-third",
    "alignment": "center",
    "max_words_per_line": 4,
    "uppercase": True,
    "active_word_highlight": True,
    "highlight_color": "#C8FF00",    # matches design system --c-lime
}


PRESETS: dict[CaptionPreset, dict[str, Any]] = {
    CaptionPreset.BOLD: _BOLD,
    CaptionPreset.MINIMAL: _MINIMAL,
    CaptionPreset.KARAOKE: _KARAOKE,
}


# ─── Public helpers ───────────────────────────────────────────────────────────

def style_config_for(preset: CaptionPreset | str) -> dict[str, Any]:
    """Return a fresh ``style_config`` dict for the given preset.

    Returns a *copy* so callers can safely mutate per-Clip overrides
    (e.g. speaker tinting) without affecting other Clips in the same
    All In Job.

    Raises ``KeyError`` if the preset is unknown — the caller should
    have validated the input through the ``CaptionPreset`` enum first.
    """
    key = preset if isinstance(preset, CaptionPreset) else CaptionPreset(preset)
    return dict(PRESETS[key])


def list_preset_names() -> list[str]:
    """Return the preset enum values in display order.

    Used by the front-end template to render the segmented control
    without hard-coding the option list in two places.
    """
    return [p.value for p in (CaptionPreset.BOLD, CaptionPreset.MINIMAL, CaptionPreset.KARAOKE)]
