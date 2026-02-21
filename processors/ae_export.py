"""
processors/ae_export.py — Generate After Effects ExtendScript (.jsx) files.

Produces a .jsx script that, when executed inside After Effects,
creates a composition with properly timed and styled text layers
for each subtitle segment, including animation keyframes.
"""

from __future__ import annotations

import re
from typing import Any


# ─── Font Mapping ────────────────────────────────────────────────────────────

AE_FONT_MAP = {
    "Bangers": "Bangers-Regular",
    "Fredoka One": "FredokaOne-Regular",
    "Bebas Neue": "BebasNeue-Regular",
    "Orbitron": "Orbitron-Bold",
    "Press Start 2P": "PressStart2P-Regular",
    "Inter": "Inter-Bold",
    "Righteous": "Righteous-Regular",
    "JetBrains Mono": "JetBrainsMono-Medium",
    "Impact": "Impact",
    "Arial": "ArialMT",
}


def _ae_font_name(css_font: str) -> str:
    """Convert CSS font-family string to AE PostScript font name."""
    # Strip quotes and fallback fonts: "'Bangers', cursive" -> "Bangers"
    name = css_font.split(",")[0].strip().strip("'\"")
    if name in AE_FONT_MAP:
        return AE_FONT_MAP[name]
    # Best guess: replace spaces with hyphens, append -Regular
    return name.replace(" ", "-") + "-Regular"


def _ae_color(hex_str: str) -> str:
    """Convert '#ffffff' to AE color array '[1, 1, 1]'."""
    h = hex_str.lstrip("#")
    if len(h) != 6:
        h = "ffffff"
    r = int(h[0:2], 16) / 255
    g = int(h[2:4], 16) / 255
    b = int(h[4:6], 16) / 255
    return f"[{r:.4f}, {g:.4f}, {b:.4f}]"


def _ae_position(
    seg: dict,
    style: dict,
    vid_w: int,
    vid_h: int,
) -> str:
    """Compute AE position [x, y] from segment or global position."""
    if seg.get("posOverride") or seg.get("pos_override"):
        px = seg.get("posX", seg.get("pos_x", 50))
        py = seg.get("posY", seg.get("pos_y", 85))
        if px is None:
            px = 50
        if py is None:
            py = 85
        x = vid_w * (px / 100)
        y = vid_h * (py / 100)
        return f"[{x:.1f}, {y:.1f}]"

    pos = style.get("position", "bottom")
    font_size = int(style.get("fontSize", 42))
    x = vid_w / 2
    if pos == "top":
        # Match ASS alignment=8: MarginV ~2.5% from top edge.
        # AE anchor is at text baseline, so add ~25% of fontSize below
        # the top margin to place text body at the same visual position.
        margin_v = round(vid_h * 0.025)
        y = margin_v + font_size * 0.75
    elif pos == "center":
        y = vid_h * 0.50
    else:  # bottom
        # Match ASS alignment=2: text bottom at vid_h - MarginV where
        # MarginV = round(vid_h * 0.025).  AE text anchor sits at the
        # baseline, roughly 25% of fontSize above the text bottom, so
        # we subtract that offset to align the visible text bottom with
        # the rendered video.
        margin_v = round(vid_h * 0.025)
        y = vid_h - margin_v - font_size * 0.25
    return f"[{x:.1f}, {y:.1f}]"


def _get_speaker_color(seg: dict, style: dict) -> str:
    """Get the hex color for a segment's speaker."""
    speaker = seg.get("speaker", "SPEAKER_00")
    speaker_styles = style.get("speakerStyles", {})
    if speaker in speaker_styles:
        val = speaker_styles[speaker]
        # Frontend sends speakerStyles as {"SPEAKER_XX": {"color": "#hex", "strokeColor": ...}}
        if isinstance(val, dict):
            return val.get("color", "#ffffff")
        return val  # fallback: already a hex string
    palette = ["#ffffff", "#FFE600", "#00F5FF", "#FF85C2", "#7FFF00", "#FF8C00"]
    idx_match = re.search(r"\d+$", speaker)
    idx = int(idx_match.group()) if idx_match else 0
    return palette[idx % len(palette)]


def _get_speaker_stroke_color(seg: dict, style: dict) -> str | None:
    """Get per-speaker stroke color override, or None to use global default."""
    speaker = seg.get("speaker", "SPEAKER_00")
    speaker_styles = style.get("speakerStyles", {})
    if speaker in speaker_styles:
        val = speaker_styles[speaker]
        if isinstance(val, dict):
            return val.get("strokeColor")
    return None


def _escape_jsx(text: str) -> str:
    """Escape string for use in ExtendScript string literals."""
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _build_animation_keyframes(
    anim_style: str,
    start_time: float,
    duration: float,
) -> str:
    """Generate ExtendScript keyframe code for segment-level animations."""
    lines = []

    if anim_style == "word-pop":
        lines.append(f"    // Word Pop: scale bounce")
        lines.append(f"    var sc = layer.property('Transform').property('Scale');")
        lines.append(f"    sc.setValueAtTime({start_time:.4f}, [30, 30]);")
        lines.append(f"    sc.setValueAtTime({start_time + 0.12:.4f}, [125, 125]);")
        lines.append(f"    sc.setValueAtTime({start_time + 0.25:.4f}, [95, 95]);")
        lines.append(f"    sc.setValueAtTime({start_time + 0.35:.4f}, [100, 100]);")
        lines.append(f"    var op = layer.property('Transform').property('Opacity');")
        lines.append(f"    op.setValueAtTime({start_time:.4f}, 0);")
        lines.append(f"    op.setValueAtTime({start_time + 0.12:.4f}, 100);")

    elif anim_style == "narration-pop":
        lines.append(f"    // Narration Pop: subtle scale pulse")
        lines.append(f"    var sc = layer.property('Transform').property('Scale');")
        lines.append(f"    sc.setValueAtTime({start_time:.4f}, [100, 100]);")
        lines.append(f"    sc.setValueAtTime({start_time + 0.08:.4f}, [115, 115]);")
        lines.append(f"    sc.setValueAtTime({start_time + 0.20:.4f}, [100, 100]);")
        lines.append(f"    var op = layer.property('Transform').property('Opacity');")
        lines.append(f"    op.setValueAtTime({start_time:.4f}, 60);")
        lines.append(f"    op.setValueAtTime({start_time + 0.08:.4f}, 100);")

    elif anim_style == "bounce-in":
        lines.append(f"    // Bounce In: position + scale bounce")
        lines.append(f"    var pos = layer.property('Transform').property('Position');")
        lines.append(f"    var basePos = pos.value;")
        lines.append(f"    pos.setValueAtTime({start_time:.4f}, [basePos[0], basePos[1] - 30]);")
        lines.append(f"    pos.setValueAtTime({start_time + 0.15:.4f}, [basePos[0], basePos[1] + 8]);")
        lines.append(f"    pos.setValueAtTime({start_time + 0.25:.4f}, [basePos[0], basePos[1] - 4]);")
        lines.append(f"    pos.setValueAtTime({start_time + 0.35:.4f}, basePos);")
        lines.append(f"    var sc = layer.property('Transform').property('Scale');")
        lines.append(f"    sc.setValueAtTime({start_time:.4f}, [80, 80]);")
        lines.append(f"    sc.setValueAtTime({start_time + 0.15:.4f}, [105, 105]);")
        lines.append(f"    sc.setValueAtTime({start_time + 0.25:.4f}, [98, 98]);")
        lines.append(f"    sc.setValueAtTime({start_time + 0.35:.4f}, [100, 100]);")
        lines.append(f"    var op = layer.property('Transform').property('Opacity');")
        lines.append(f"    op.setValueAtTime({start_time:.4f}, 0);")
        lines.append(f"    op.setValueAtTime({start_time + 0.10:.4f}, 100);")

    elif anim_style == "slide-up":
        lines.append(f"    // Slide Up: position + opacity")
        lines.append(f"    var pos = layer.property('Transform').property('Position');")
        lines.append(f"    var basePos = pos.value;")
        lines.append(f"    pos.setValueAtTime({start_time:.4f}, [basePos[0], basePos[1] + 20]);")
        lines.append(f"    pos.setValueAtTime({start_time + 0.30:.4f}, basePos);")
        lines.append(f"    var sc = layer.property('Transform').property('Scale');")
        lines.append(f"    sc.setValueAtTime({start_time:.4f}, [105, 105]);")
        lines.append(f"    sc.setValueAtTime({start_time + 0.30:.4f}, [100, 100]);")
        lines.append(f"    var op = layer.property('Transform').property('Opacity');")
        lines.append(f"    op.setValueAtTime({start_time:.4f}, 0);")
        lines.append(f"    op.setValueAtTime({start_time + 0.20:.4f}, 100);")

    elif anim_style == "zoom-flash":
        lines.append(f"    // Zoom Flash: dramatic scale down + brightness")
        lines.append(f"    var sc = layer.property('Transform').property('Scale');")
        lines.append(f"    sc.setValueAtTime({start_time:.4f}, [250, 250]);")
        lines.append(f"    sc.setValueAtTime({start_time + 0.20:.4f}, [100, 100]);")
        lines.append(f"    var op = layer.property('Transform').property('Opacity');")
        lines.append(f"    op.setValueAtTime({start_time:.4f}, 0);")
        lines.append(f"    op.setValueAtTime({start_time + 0.10:.4f}, 100);")

    elif anim_style == "typewriter":
        lines.append(f"    // Typewriter: clean fade in")
        lines.append(f"    var op = layer.property('Transform').property('Opacity');")
        lines.append(f"    op.setValueAtTime({start_time:.4f}, 0);")
        lines.append(f"    op.setValueAtTime({start_time + 0.18:.4f}, 100);")

    elif anim_style == "karaoke":
        lines.append(f"    // Karaoke: fade in then highlight words via expression")
        lines.append(f"    var op = layer.property('Transform').property('Opacity');")
        lines.append(f"    op.setValueAtTime({start_time:.4f}, 0);")
        lines.append(f"    op.setValueAtTime({start_time + 0.10:.4f}, 100);")

    else:
        # Default simple fade
        lines.append(f"    var op = layer.property('Transform').property('Opacity');")
        lines.append(f"    op.setValueAtTime({start_time:.4f}, 0);")
        lines.append(f"    op.setValueAtTime({start_time + 0.15:.4f}, 100);")

    # Fade out at end for all animation types — `op` is always defined above
    end_time = start_time + duration
    if duration > 0.3:
        lines.append(f"    // Fade out")
        lines.append(f"    op.setValueAtTime({end_time - 0.10:.4f}, 100);")
        lines.append(f"    op.setValueAtTime({end_time:.4f}, 0);")

    return "\n".join(lines)


def generate_ae_script(
    segments: list[dict],
    style_config: dict,
    video_width: int = 1920,
    video_height: int = 1080,
    video_duration: float = 60.0,
    fps: float = 30.0,
) -> str:
    """
    Generate a complete After Effects ExtendScript (.jsx) file.

    Parameters
    ----------
    segments : list[dict]
        Transcript segments with {start, end, text, speaker, words?, posOverride?, posX?, posY?}
    style_config : dict
        Style configuration from the frontend (fontFamily, fontSize, fontColor, etc.)
    video_width : int
        Video width in pixels
    video_height : int
        Video height in pixels
    video_duration : float
        Total video duration in seconds
    fps : float
        Frames per second

    Returns
    -------
    str
        Complete .jsx ExtendScript content
    """
    font_name = _ae_font_name(style_config.get("fontFamily", "'Inter', sans-serif"))
    font_size = style_config.get("fontSize", 42)
    font_color = style_config.get("fontColor", "#ffffff")
    stroke_enabled = style_config.get("strokeEnabled", False)
    stroke_color = style_config.get("strokeColor", "#000000")
    stroke_width = style_config.get("strokeWidth", 3)
    glow_enabled = style_config.get("glowEnabled", False)
    glow_color = style_config.get("glowColor", "#ff006e")
    glow_blur = style_config.get("glowBlur", 12)
    anim_style = style_config.get("animStyle", "word-pop")

    jsx_lines = []

    # Header
    jsx_lines.append("// ═══════════════════════════════════════════════════════════")
    jsx_lines.append("// CLIP-AUTOMATION — After Effects Subtitle Script")
    jsx_lines.append("// Generated by CLIP-AUTOMATION Auto-Subtitle System")
    jsx_lines.append("// ═══════════════════════════════════════════════════════════")
    jsx_lines.append("//")
    jsx_lines.append("// HOW TO USE:")
    jsx_lines.append("// 1. Open After Effects")
    jsx_lines.append("// 2. File > Scripts > Run Script File...")
    jsx_lines.append("// 3. Select this .jsx file")
    jsx_lines.append("// 4. The script will create a composition with all subtitles")
    jsx_lines.append("// 5. Import your video and place it below the subtitle layers")
    jsx_lines.append("//")
    jsx_lines.append(f"// Required Font: {font_name}")
    jsx_lines.append(f"// Animation Style: {anim_style}")
    jsx_lines.append(f"// Total Segments: {len(segments)}")
    jsx_lines.append("// ═══════════════════════════════════════════════════════════")
    jsx_lines.append("")
    jsx_lines.append("(function() {")
    jsx_lines.append('    app.beginUndoGroup("CLIP-AUTO Subtitles");')
    jsx_lines.append("")

    # Create composition
    jsx_lines.append("    // ── Create Composition ──")
    jsx_lines.append(f'    var comp = app.project.items.addComp("CLIP-AUTO Subtitles", {video_width}, {video_height}, 1, {video_duration:.4f}, {fps});')
    jsx_lines.append(f"    comp.bgColor = [0, 0, 0];")
    jsx_lines.append("")

    # Add a guide text layer with instructions
    jsx_lines.append("    // ── Guide Layer (can be deleted) ──")
    jsx_lines.append('    var guide = comp.layers.addText("Import your video and place it below these subtitle layers");')
    jsx_lines.append(f"    guide.inPoint = 0;")
    jsx_lines.append(f"    guide.outPoint = 3;")
    jsx_lines.append("    var guideProp = guide.property('Source Text');")
    jsx_lines.append("    var guideDoc = guideProp.value;")
    jsx_lines.append("    guideDoc.fontSize = 24;")
    jsx_lines.append('    guideDoc.font = "ArialMT";')
    jsx_lines.append("    guideDoc.fillColor = [0.5, 0.5, 0.5];")
    jsx_lines.append("    guideDoc.justification = ParagraphJustification.CENTER_JUSTIFY;")
    jsx_lines.append("    guideProp.setValue(guideDoc);")
    jsx_lines.append(f"    guide.property('Transform').property('Position').setValue([{video_width/2}, {video_height/2}]);")
    jsx_lines.append('    guide.name = "-- GUIDE (delete me) --";')
    jsx_lines.append("")

    # Create text layers for each segment (in reverse order so first segment is on top)
    jsx_lines.append(f"    // ── Create {len(segments)} Subtitle Layers ──")
    jsx_lines.append("")

    for i, seg in enumerate(segments):
        seg_text = seg.get("text", "").strip()
        if not seg_text:
            continue

        start = seg.get("start", 0)
        end = seg.get("end", 0)
        duration = end - start
        if duration <= 0:
            continue

        speaker = seg.get("speaker", "SPEAKER_00")
        speaker_color = _get_speaker_color(seg, style_config)
        position = _ae_position(seg, style_config, video_width, video_height)

        jsx_lines.append(f"    // ── Segment {i + 1}: [{_fmt_time(start)} - {_fmt_time(end)}] ──")
        jsx_lines.append(f"    (function() {{")
        jsx_lines.append(f'        var layer = comp.layers.addText("{_escape_jsx(seg_text)}");')
        jsx_lines.append(f'        layer.name = "Sub {i + 1}: {_escape_jsx(seg_text[:30])}";')
        jsx_lines.append(f"        layer.inPoint = {start:.4f};")
        jsx_lines.append(f"        layer.outPoint = {end:.4f};")
        jsx_lines.append(f"")

        # Text document properties
        jsx_lines.append(f"        // Text styling")
        jsx_lines.append(f"        var textProp = layer.property('Source Text');")
        jsx_lines.append(f"        var textDoc = textProp.value;")
        jsx_lines.append(f"        textDoc.fontSize = {font_size};")
        jsx_lines.append(f'        textDoc.font = "{font_name}";')
        jsx_lines.append(f"        textDoc.fillColor = {_ae_color(speaker_color)};")
        jsx_lines.append(f"        textDoc.justification = ParagraphJustification.CENTER_JUSTIFY;")

        if stroke_enabled and stroke_width > 0:
            # Per-speaker stroke color override (matches preview/render behavior)
            seg_stroke_color = _get_speaker_stroke_color(seg, style_config) or stroke_color
            # AE stroke with strokeOverFill=false renders stroke BEHIND fill,
            # matching the CSS text-shadow outline used in preview/render.
            # Multiply width by 2 because AE stroke extends half outward / half
            # inward; with strokeOverFill=false the inner half is hidden by fill,
            # so visible outline = strokeWidth/2. CSS shadow offset = full width.
            ae_stroke_w = stroke_width * 2
            jsx_lines.append(f"        textDoc.applyStroke = true;")
            jsx_lines.append(f"        textDoc.strokeColor = {_ae_color(seg_stroke_color)};")
            jsx_lines.append(f"        textDoc.strokeWidth = {ae_stroke_w};")
            jsx_lines.append(f"        textDoc.strokeOverFill = false;")

        jsx_lines.append(f"        textProp.setValue(textDoc);")
        jsx_lines.append(f"")

        # Position
        jsx_lines.append(f"        // Position")
        jsx_lines.append(f"        layer.property('Transform').property('Position').setValue({position});")
        jsx_lines.append(f"")

        # Glow effect — two stacked Drop Shadow effects at distance=0 to
        # approximate the double-layer CSS text-shadow used in preview.
        if glow_enabled and glow_blur > 0:
            jpg_lines = [
                f"        // Glow effect (two Drop Shadows at distance 0, matching preview)",
                f"        var ds1 = layer.property('Effects').addProperty('ADBE Drop Shadow');",
                f"        ds1.property('Shadow Color').setValue({_ae_color(glow_color)});",
                f"        ds1.property('Opacity').setValue(100);",
                f"        ds1.property('Direction').setValue(0);",
                f"        ds1.property('Distance').setValue(0);",
                f"        ds1.property('Softness').setValue({glow_blur});",
                f"        var ds2 = layer.property('Effects').addProperty('ADBE Drop Shadow');",
                f"        ds2.property('Shadow Color').setValue({_ae_color(glow_color)});",
                f"        ds2.property('Opacity').setValue(80);",
                f"        ds2.property('Direction').setValue(0);",
                f"        ds2.property('Distance').setValue(0);",
                f"        ds2.property('Softness').setValue({glow_blur * 2});",
            ]
            jsx_lines.extend(jpg_lines)
            jsx_lines.append(f"")

        # Animation keyframes
        jsx_lines.append(f"        // Animation: {anim_style}")
        keyframe_code = _build_animation_keyframes(anim_style, start, duration)
        # Indent for inner IIFE
        for kf_line in keyframe_code.split("\n"):
            jsx_lines.append(f"    {kf_line}")

        jsx_lines.append(f"    }})();")
        jsx_lines.append(f"")

    # Footer
    jsx_lines.append("    app.endUndoGroup();")
    jsx_lines.append(f'    alert("CLIP-AUTO: Created {len(segments)} subtitle layers in comp \\"{_escape_jsx("CLIP-AUTO Subtitles")}\\"\\n\\nRemember to import your video and place it below the subtitle layers.");')
    jsx_lines.append("})();")

    return "\n".join(jsx_lines)


def _fmt_time(secs: float) -> str:
    """Format seconds to MM:SS.s"""
    m = int(secs // 60)
    s = secs % 60
    return f"{m}:{s:05.2f}"
