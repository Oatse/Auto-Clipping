"""
processors/subtitle_renderer.py — Phase 3: Pycaps Subtitle Injection.

Responsibilities:
  1. Build a Pycaps-compatible word-level JSON from WhisperX word timestamps
     + global video segment offsets.
  2. Bypass Pycaps internal transcription entirely.
  3. Apply CSS styling derived from the UI style_config dict.
  4. Single-speaker: render via Pycaps CapsPipelineBuilder.
  5. Multi-speaker: generate ASS subtitle file + burn via FFmpeg
     (ASS natively supports simultaneous multi-line subtitles + per-speaker colors).

Formula:
    Global_Word_Start = Segment_Start_Time_In_Video + WhisperX_Word_Start_Time
"""

from __future__ import annotations

import json
from pathlib import Path

from loguru import logger

from models.transcript import (
    PycapsWordEntry,
    TranscriptSegment,
)
from utils.file_utils import ensure_dir


def _get_video_dimensions(video_path: Path) -> tuple[int, int]:
    """
    Probe a video file for its native width and height.
    Falls back to 1920×1080 if ffprobe fails or the stream is unavailable.
    """
    import subprocess

    from utils.ffmpeg_utils import FFPROBE_BIN

    cmd = [
        FFPROBE_BIN,
        "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        "-select_streams", "v:0",
        str(video_path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.returncode == 0:
            data = json.loads(result.stdout)
            streams = data.get("streams", [])
            if streams:
                w = int(streams[0].get("width", 1920))
                h = int(streams[0].get("height", 1080))
                return w, h
    except Exception:
        pass
    return 1920, 1080  # safe fallback


def _build_css_from_style(style: dict) -> str:
    """
    Convert a style_config dict (from the UI) into Pycaps-compatible CSS.

    style_config keys (all optional, with defaults):
        fontFamily      str   e.g. "'Bangers', cursive"
        fontSize        int   pixels, e.g. 54
        fontColor       str   hex, e.g. "#ffffff"
        strokeEnabled   bool
        strokeColor     str   hex
        strokeWidth     int   pixels
        glowEnabled     bool
        glowColor       str   hex
        glowBlur        int   pixels
        bgBoxEnabled    bool
        bgBoxColor      str   hex
        bgOpacity       int   0–100
        animStyle       str   animation style name
    """
    font_family = style.get("fontFamily", "Arial, sans-serif")
    font_size   = int(style.get("fontSize", 40))
    font_color  = style.get("fontColor", "#ffffff")

    stroke_enabled = bool(style.get("strokeEnabled", False))
    stroke_color   = style.get("strokeColor", "#000000")
    stroke_width   = int(style.get("strokeWidth", 3))

    glow_enabled = bool(style.get("glowEnabled", False))
    glow_color   = style.get("glowColor", "#ffffff")
    glow_blur    = int(style.get("glowBlur", 10))

    bg_box_enabled = bool(style.get("bgBoxEnabled", False))
    bg_box_color   = style.get("bgBoxColor", "#000000")
    bg_opacity     = int(style.get("bgOpacity", 60))

    # Animasi WORD-level (per kata saat diucapkan) mendapat effect scale pada .word-being-narrated
    # Animasi SEGMENT-level (entry semua kata bersamaan) tidak — kata sudah statik setelah muncul
    anim_style = style.get("animStyle", "word-pop")
    NARRATION_ANIMS = {"narration-pop", "zoom-flash", "typewriter", "karaoke"}
    is_narration_anim = anim_style in NARRATION_ANIMS

    # Build text-shadow: stroke (via multi-shadow outline) + glow
    shadows = []
    if stroke_enabled and stroke_width > 0:
        w = stroke_width
        c = stroke_color
        shadows += [
            f"{w}px {w}px 0 {c}",
            f"-{w}px {w}px 0 {c}",
            f"{w}px -{w}px 0 {c}",
            f"-{w}px -{w}px 0 {c}",
            f"{w}px 0 0 {c}",
            f"-{w}px 0 0 {c}",
            f"0 {w}px 0 {c}",
            f"0 -{w}px 0 {c}",
        ]
    if glow_enabled and glow_blur > 0:
        shadows += [
            f"0 0 {glow_blur}px {glow_color}",
            f"0 0 {glow_blur * 2}px {glow_color}",
        ]

    text_shadow_css = f"text-shadow: {', '.join(shadows)};" if shadows else ""

    # Background box
    bg_parts = []
    if bg_box_enabled:
        r = int(bg_box_color[1:3], 16)
        g = int(bg_box_color[3:5], 16)
        b = int(bg_box_color[5:7], 16)
        a = round(bg_opacity / 100, 2)
        bg_parts = [
            f"background: rgba({r},{g},{b},{a});",
            "padding: 4px 10px;",
            "border-radius: 6px;",
        ]
    bg_css = " ".join(bg_parts)

    # Padding: when bg box is enabled its padding takes priority; otherwise default word padding
    word_padding = "" if bg_box_enabled else "padding: 2px 6px;"

    # .word-being-narrated: scale only for narration-based animations
    being_narrated_extra = "transform: scale(1.08); transition: transform 0.05s ease;" if is_narration_anim else ""

    css = f"""
.word {{
    font-family: {font_family};
    font-size: {font_size}px;
    color: {font_color};
    font-weight: 800;
    display: inline-block;
    {text_shadow_css}
    {bg_css}
    {word_padding}
}}

.word-being-narrated {{
    color: {font_color};
    {being_narrated_extra}
}}

.word-already-narrated {{
    color: {font_color};
    opacity: 0.75;
}}
"""
    return css


def _get_pycaps_animation(anim_style: str):
    """
    Map a UI animation style name to a (Animation, ElementType) tuple.

    Returns (animation, element_type) where:
    - element_type=SEGMENT → animation fires once when the whole subtitle line appears
    - element_type=WORD    → animation fires per-word when that word is being narrated

    "word-pop"     = entry animation (all words pop in together when segment appears)
    "narration-pop"= per-word pop exactly when each word is spoken
    """
    try:
        from pycaps.animation.builtin.preset.pop_in import PopIn
        from pycaps.animation.builtin.preset.pop_in_bounce import PopInBounce
        from pycaps.animation.builtin.preset.slide_in import SlideIn
        from pycaps.animation.builtin.preset.zoom_in import ZoomIn
        from pycaps.animation.builtin.preset.fade_in import FadeIn
        from pycaps.animation.definitions import Direction
        from pycaps.common import ElementType
    except ImportError:
        return None, None

    # (animation, element_type)
    mapping = {
        "word-pop":      (PopIn(duration=0.3),                       ElementType.SEGMENT),
        "narration-pop": (PopIn(duration=0.25),                      ElementType.WORD),
        "bounce-in":     (PopInBounce(duration=0.35),                ElementType.SEGMENT),
        "slide-up":      (SlideIn(direction=Direction.UP, duration=0.3), ElementType.SEGMENT),
        "zoom-flash":    (ZoomIn(duration=0.25),                     ElementType.WORD),
        "typewriter":    (FadeIn(duration=0.15),                     ElementType.WORD),
        "karaoke":       (FadeIn(duration=0.15),                     ElementType.WORD),
    }
    return mapping.get(anim_style, (PopIn(duration=0.3), ElementType.SEGMENT))


# Speaker color palette — matches frontend SPEAKER_COLORS in app.js
_SPEAKER_COLORS_RGB = [
    (255, 255, 255),   # SPEAKER_00 — white
    (255, 230,   0),   # SPEAKER_01 — yellow
    (  0, 245, 255),   # SPEAKER_02 — cyan
    (255, 133, 194),   # SPEAKER_03 — pink
    (127, 255,   0),   # SPEAKER_04 — lime
    (255, 140,   0),   # SPEAKER_05 — orange
]


def _speaker_index(speaker_id: str) -> int:
    import re
    m = re.search(r"\d+$", speaker_id or "")
    return int(m.group()) % len(_SPEAKER_COLORS_RGB) if m else 0


def _rgb_to_ass(r: int, g: int, b: int, alpha: int = 0) -> str:
    """Convert RGB to ASS color format &HAABBGGRR."""
    return f"&H{alpha:02X}{b:02X}{g:02X}{r:02X}"


def _secs_to_ass_time(secs: float) -> str:
    """Convert seconds to ASS timestamp h:mm:ss.cc"""
    h = int(secs // 3600)
    m = int((secs % 3600) // 60)
    s = int(secs % 60)
    cs = int(round((secs % 1) * 100))
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _get_ass_anim_tag(anim_style: str) -> str:
    """
    Return ASS override tag string for entry animation effect.
    Prepended to each Dialogue line text field.

    Uses ASS \fad (fade) + \t (transform/scale) to simulate pop-in animations.
    The \t tag animates from start to end time in milliseconds.
    """
    tags = {
        # Pop: fast fade + scale up then settle back — mimics Pycaps word-pop
        "word-pop":      r"{\fad(80,100)\t(0,150,\fscx112\fscy112)\t(150,260,\fscx100\fscy100)}",
        # Narration pop: same pop feel
        "narration-pop": r"{\fad(80,100)\t(0,150,\fscx112\fscy112)\t(150,260,\fscx100\fscy100)}",
        # Bounce: pop with overshoot (3-stage scale)
        "bounce-in":     r"{\fad(60,80)\t(0,100,\fscx122\fscy122)\t(100,190,\fscx95\fscy95)\t(190,270,\fscx102\fscy102)\t(270,330,\fscx100\fscy100)}",
        # Slide up: smooth fade + gentle scale grow
        "slide-up":      r"{\fad(160,100)\t(0,220,\fscx105\fscy105)\t(220,320,\fscx100\fscy100)}",
        # Zoom flash: sharp zoom then snap back
        "zoom-flash":    r"{\fad(40,80)\t(0,100,\fscx125\fscy125)\t(100,200,\fscx100\fscy100)}",
        # Typewriter / karaoke: clean fade only
        "typewriter":    r"{\fad(180,100)}",
        "karaoke":       r"{\fad(150,100)}",
    }
    return tags.get(anim_style, r"{\fad(100,80)\t(0,150,\fscx110\fscy110)\t(150,250,\fscx100\fscy100)}")


def _build_ass_content(
    segments: list,
    style: dict,
    video_width: int = 1920,
    video_height: int = 1080,
) -> str:
    """
    Generate an ASS subtitle file supporting multiple simultaneous speakers.

    Each unique speaker gets:
    - A dedicated ASS Style with their speaker color
    - Separate vertical positioning via MarginV so they stack without overlap

    video_width / video_height must match the native video resolution so that
    PlayResX / PlayResY are set correctly and font sizes (already scaled to
    native pixels by the frontend) render at the expected proportional size.
    """
    font_name = style.get("fontFamily", "Arial, sans-serif").split(",")[0].strip().strip("'\"")
    font_size = int(style.get("fontSize", 40))

    stroke_enabled = bool(style.get("strokeEnabled", False))
    stroke_width   = int(style.get("strokeWidth", 3)) if stroke_enabled else 0
    stroke_color   = style.get("strokeColor", "#000000")

    glow_enabled = bool(style.get("glowEnabled", False))
    glow_blur    = int(style.get("glowBlur", 10)) if glow_enabled else 0

    bg_box_enabled = bool(style.get("bgBoxEnabled", False))
    bg_box_color   = style.get("bgBoxColor", "#000000")
    bg_opacity     = int(style.get("bgOpacity", 60)) if bg_box_enabled else 0

    position_str = style.get("position", "bottom")

    # Collect unique speakers in order of first appearance
    seen: list[str] = []
    for seg in segments:
        sp = getattr(seg, "speaker", None) or (seg.get("speaker") if isinstance(seg, dict) else "SPEAKER_00") or "SPEAKER_00"
        if sp not in seen:
            seen.append(sp)

    # ASS alignment: 2=bottom-center, 8=top-center, 5=middle-center
    alignment = {"bottom": 2, "top": 8, "center": 5}.get(position_str, 2)

    # Outline color from stroke
    def hex_to_rgb(h: str):
        h = h.lstrip("#")
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)

    out_r, out_g, out_b = hex_to_rgb(stroke_color)
    outline_color = _rgb_to_ass(out_r, out_g, out_b)

    # Back color (glow / box background)
    if bg_box_enabled:
        bx_r, bx_g, bx_b = hex_to_rgb(bg_box_color)
        back_alpha_val = int((1 - bg_opacity / 100) * 255)
        back_color = _rgb_to_ass(bx_r, bx_g, bx_b, back_alpha_val)
    elif glow_enabled:
        g_r, g_g, g_b = hex_to_rgb(style.get("glowColor", "#ffffff"))
        back_color = _rgb_to_ass(g_r, g_g, g_b, 50)
    else:
        back_color = "&H80000000"

    border_style = 3 if bg_box_enabled else 1  # 1=outline, 3=opaque box
    outline_size = stroke_width
    shadow_size  = glow_blur // 4 if glow_enabled else 0

    # Build style lines — one per speaker, all use margin_v_base as default.
    # Actual per-line margins are set dynamically in the Dialogue lines below.
    style_lines = []
    # Margins are in the script coordinate space (=video native pixels since
    # PlayResX/PlayResY will be set to the video's native dimensions).
    margin_v_base = round(video_height * 0.025)      # ~27 px at 1080p
    margin_v_step = font_size + round(video_height * 0.012)  # gap between stacked speakers

    # Per-speaker color overrides from UI (style_config["speakerStyles"])
    # Format: { "SPEAKER_00": {"color": "#rrggbb", "strokeColor": "#rrggbb"|null}, ... }
    speaker_color_overrides: dict = style.get("speakerStyles", {})

    def _resolve_speaker_color(speaker: str) -> tuple[int, int, int]:
        """Return (R, G, B) for a speaker, using UI override if present."""
        override = speaker_color_overrides.get(speaker)
        if override:
            # Frontend sends speakerStyles as {"SPEAKER_XX": {"color": "#hex", "strokeColor": ...}}
            color_hex = override.get("color", "#ffffff") if isinstance(override, dict) else override
            h = color_hex.lstrip("#")
            return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        idx = _speaker_index(speaker)
        return _SPEAKER_COLORS_RGB[idx]

    def _resolve_speaker_stroke_color(speaker: str) -> str:
        """Return ASS outline colour for a speaker.

        Uses the per-speaker strokeColor override from speakerStyles when
        available; otherwise falls back to the global outline_color.
        """
        override = speaker_color_overrides.get(speaker)
        if override and isinstance(override, dict):
            sc = override.get("strokeColor")
            if sc:
                sr, sg, sb = hex_to_rgb(sc)
                return _rgb_to_ass(sr, sg, sb)
        return outline_color

    for speaker in seen:
        r, g, b = _resolve_speaker_color(speaker)
        primary  = _rgb_to_ass(r, g, b)
        speaker_outline = _resolve_speaker_stroke_color(speaker)

        # Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour,
        #         OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut,
        #         ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow,
        #         Alignment, MarginL, MarginR, MarginV, Encoding
        style_lines.append(
            f"Style: {speaker},{font_name},{font_size},{primary},&H00FFFFFF,"
            f"{speaker_outline},{back_color},-1,0,0,0,"
            f"100,100,0,0,{border_style},{outline_size},{shadow_size},"
            f"{alignment},10,10,{margin_v_base},1"
        )

    # Pre-extract (start, end, speaker) for every segment for overlap queries
    def _seg_fields(s) -> tuple[float, float, str]:
        if isinstance(s, dict):
            return (
                s.get("start", 0),
                s.get("end", 0),
                s.get("speaker", "SPEAKER_00") or "SPEAKER_00",
            )
        return (
            s.start,
            s.end,
            getattr(s, "speaker", "SPEAKER_00") or "SPEAKER_00",
        )

    all_fields = [_seg_fields(s) for s in segments]

    # ── Fix same-speaker overlaps before building dialogue lines ──────────
    # Sort segments by (speaker, start) and trim any same-speaker overlaps
    # so subtitles for one speaker never stack on top of each other.
    _speaker_groups: dict[str, list[int]] = {}
    for _i, (_, _, _sp) in enumerate(all_fields):
        _speaker_groups.setdefault(_sp, []).append(_i)

    for _sp, _indices in _speaker_groups.items():
        # Sort indices by start time
        _indices.sort(key=lambda _j: all_fields[_j][0])
        for _k in range(len(_indices) - 1):
            cur_idx = _indices[_k]
            nxt_idx = _indices[_k + 1]
            cur_start, cur_end, _ = all_fields[cur_idx]
            nxt_start, nxt_end, _ = all_fields[nxt_idx]
            if cur_end > nxt_start:
                # Trim current segment's end to avoid overlap
                new_end = max(cur_start + 0.05, nxt_start - 0.01)
                all_fields[cur_idx] = (cur_start, new_end, _sp)
                # Also update the actual segment object
                _seg_obj = segments[cur_idx]
                if isinstance(_seg_obj, dict):
                    _seg_obj["end"] = new_end
                else:
                    _seg_obj.end = new_end
                logger.debug(
                    "ASS: trimmed same-speaker overlap for {} seg#{}: "
                    "end {:.3f} → {:.3f}",
                    _sp, cur_idx, cur_end, new_end,
                )

    # Build dialogue lines with dynamic per-line MarginV
    dialogue_lines = []
    for idx, seg in enumerate(segments):
        start, end, sp = all_fields[idx]

        text = (seg.get("text", "") if isinstance(seg, dict) else seg.text).strip()
        if not text:
            continue

        # Find all speakers whose segments overlap [start, end]
        simultaneous: set[str] = {sp}
        for o_start, o_end, o_sp in all_fields:
            if o_sp != sp and o_start < end and o_end > start:
                simultaneous.add(o_sp)

        # Sort overlapping speakers by numeric index for consistent ordering.
        # Lowest index → stack position 0 (bottom); next → position 1, etc.
        sorted_sim = sorted(simultaneous, key=_speaker_index)
        stack_pos = sorted_sim.index(sp)  # 0 = bottom

        line_margin_v = margin_v_base + stack_pos * margin_v_step

        # ASS escapes: curly braces are control codes
        text_esc = text.replace("{", "\\\{").replace("}", "\\\}")
        anim_tag = _get_ass_anim_tag(style.get("animStyle", "word-pop"))
        dialogue_lines.append(
            f"Dialogue: 0,{_secs_to_ass_time(start)},{_secs_to_ass_time(end)},"
            f"{sp},,0,0,{line_margin_v},,{anim_tag}{text_esc}"
        )

    styles_block = "\n".join(style_lines)
    dialogue_block = "\n".join(dialogue_lines)

    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: {video_width}
PlayResY: {video_height}
Collisions: Normal
PlayDepth: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
{styles_block}

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
{dialogue_block}
"""


class SubtitleRendererProcessor:
    """
    Phase 3: Build Pycaps-compatible subtitle JSON and render onto video.

    Bypasses Pycaps' internal transcription by injecting a pre-built
    word-level JSON derived from WhisperX word alignment data.
    """

    def build_pycaps_transcript(
        self,
        segments: list[TranscriptSegment],
        output_dir: Path | str,
    ) -> Path:
        """
        Build and save the Pycaps-compatible word-level transcript JSON.

        For each word in each segment:
            global_start = segment.start + whisperx_word.start
            global_end   = segment.start + whisperx_word.end

        Parameters
        ----------
        segments:
            Transcript segments with WhisperX word timestamps.
        output_dir:
            Directory to save the pycaps_transcript.json file.

        Returns
        -------
        Path
            Path to the generated pycaps_transcript.json.
        """
        output_dir = Path(output_dir)
        ensure_dir(output_dir)

        segment_entries: list[dict] = []
        total_words = 0

        for seg in segments:
            seg_words: list[dict] = []
            for word_ts in seg.words:
                seg_words.append(
                    PycapsWordEntry(
                        word=word_ts.word,
                        global_start=word_ts.start,
                        global_end=word_ts.end,
                    ).to_dict()
                )
            if seg_words:
                segment_entries.append({"words": seg_words})
                total_words += len(seg_words)

        # pycaps expects a top-level object with a "segments" array.
        pycaps_data = {"segments": segment_entries}

        json_path = output_dir / "pycaps_transcript.json"
        with json_path.open("w", encoding="utf-8") as f:
            json.dump(pycaps_data, f, indent=2, ensure_ascii=False)

        logger.info(
            "Built Pycaps transcript: {} words → {}",
            total_words,
            json_path,
        )
        return json_path

    def render(
        self,
        video_path: Path | str,
        pycaps_transcript: Path | str,
        output_path: Path | str,
        style_config: dict | None = None,
        segments: list | None = None,
        speaker_detection: bool = True,
    ) -> Path:
        """
        Render subtitles onto the video.

        Single-speaker with speaker_detection=True: uses Pycaps CapsPipelineBuilder
            (supports animations).
        Multi-speaker OR speaker_detection=False: generates an ASS subtitle file
            and burns it via FFmpeg (ASS natively supports simultaneous multi-line
            subtitles with per-speaker colors and vertical stacking).

        Parameters
        ----------
        video_path:
            Input video (from Phase 5 output or original).
        pycaps_transcript:
            Path to pycaps_transcript.json built by build_pycaps_transcript().
        output_path:
            Output video with burned-in subtitles.
        style_config:
            Dict from the UI with keys like fontFamily, fontSize, fontColor,
            strokeEnabled, strokeColor, strokeWidth, glowEnabled, glowColor,
            glowBlur, bgBoxEnabled, bgBoxColor, bgOpacity, animStyle, position.
        segments:
            TranscriptSegment list used for speaker diarization (multi-speaker
            detection). When more than one unique speaker is found the render
            falls back to the ASS/FFmpeg path.
        speaker_detection:
            When False (speaker detection was disabled at transcription time),
            force the ASS/FFmpeg render path regardless of speaker count so
            rendering behaviour is consistent with the multi-speaker path.

        Returns
        -------
        Path
            Path to the subtitle-rendered video.
        """
        video_path   = Path(video_path)
        output_path  = Path(output_path)
        ensure_dir(output_path.parent)

        style = style_config or {}
        segs  = segments or []

        # ── Speaker detection ─────────────────────────────────────────────────
        unique_speakers = list(dict.fromkeys(
            (getattr(s, "speaker", None) or "SPEAKER_00")
            for s in segs
        ))
        logger.info(
            "Rendering subtitles: {} → {} (style: font={}, anim={}, speakers={})",
            video_path.name,
            output_path.name,
            style.get("fontFamily", "default"),
            style.get("animStyle", "word-pop"),
            len(unique_speakers) if unique_speakers else 1,
        )

        # Always use ASS/FFmpeg path for all cases (single-speaker, multi-speaker,
        # speaker detection on or off) for consistent rendering behaviour.
        if segs:
            return self._render_ass(video_path, output_path, segs, style)

        # ── Single-speaker path: Pycaps ───────────────────────────────────────
        try:
            from pycaps import CapsPipelineBuilder  # type: ignore
            from pycaps.layout import SubtitleLayoutOptions, VerticalAlignment, VerticalAlignmentType
            from pycaps.common import ElementType, EventType
        except ImportError:
            raise ImportError(
                "pycaps is not installed. Install it with: pip install git+https://github.com/francozanardi/pycaps.git"
            )

        pycaps_transcript = Path(pycaps_transcript)

        # ── Compensate for Pycaps device_scale_factor (DSF) ───────────────────
        # Pycaps' CssSubtitleRenderer renders word images at DSF× the CSS pixel
        # size, then composites them onto the video at native resolution.  This
        # effectively multiplies all CSS pixel sizes by DSF.  The frontend has
        # already scaled fontSize / strokeWidth / glowBlur from preview-pixels
        # to native-video-pixels (nativeWidth / displayedWidth), but that
        # scaling assumes 1 CSS-px = 1 video-px.  Because Pycaps inserts an
        # extra DSF multiplier we must divide by DSF so the final composited
        # size matches the proportional size the user saw in the preview.
        #
        # DSF formula (mirrors CssSubtitleRenderer internals):
        #   scale_modifier = clamp(video_height / 1280, 0.25, 5.0)
        #   device_scale_factor = 2.0 × scale_modifier
        vid_w, vid_h = _get_video_dimensions(video_path)
        _pycaps_ref_h = 1280
        _pycaps_base_dsf = 2.0
        _scale_mod = max(0.25, min(5.0, vid_h / _pycaps_ref_h))
        _pycaps_dsf = _pycaps_base_dsf * _scale_mod

        pycaps_style = dict(style)
        if _pycaps_dsf > 0:
            pycaps_style["fontSize"]    = max(1, round(int(pycaps_style.get("fontSize", 40)) / _pycaps_dsf))
            pycaps_style["strokeWidth"] = max(0, round(int(pycaps_style.get("strokeWidth", 3)) / _pycaps_dsf))
            pycaps_style["glowBlur"]    = max(0, round(int(pycaps_style.get("glowBlur", 10)) / _pycaps_dsf))

        logger.debug(
            "Pycaps DSF correction: DSF={:.4f}, fontSize {} → {}, strokeWidth {} → {}, glowBlur {} → {}",
            _pycaps_dsf,
            style.get("fontSize"), pycaps_style["fontSize"],
            style.get("strokeWidth"), pycaps_style["strokeWidth"],
            style.get("glowBlur"), pycaps_style["glowBlur"],
        )

        # Build CSS from DSF-corrected style config
        css_content = _build_css_from_style(pycaps_style)

        # Map position string to Pycaps VerticalAlignmentType
        pos_str = style.get("position", "bottom")
        pos_map = {
            "bottom": VerticalAlignmentType.BOTTOM,
            "center": VerticalAlignmentType.CENTER,
            "top":    VerticalAlignmentType.TOP,
        }
        v_align_type = pos_map.get(pos_str, VerticalAlignmentType.BOTTOM)

        layout_options = SubtitleLayoutOptions(
            max_width_ratio=0.85,
            max_number_of_lines=2,
            vertical_align=VerticalAlignment(align=v_align_type, offset=-0.05),
        )

        builder = CapsPipelineBuilder()

        if hasattr(builder, "with_input_video"):
            builder = builder.with_input_video(str(video_path))
        elif hasattr(builder, "load_video"):
            builder = builder.load_video(str(video_path))
        else:
            raise AttributeError(
                "Unsupported pycaps CapsPipelineBuilder API: missing "
                "'with_input_video' and 'load_video'."
            )

        builder = (
            builder
            .with_transcription_file(str(pycaps_transcript))
            .add_css_content(css_content)
            .with_layout_options(layout_options)
            .with_output_video(str(output_path))
        )

        # Add word entry animation
        anim_style = style.get("animStyle", "word-pop")
        animation, element_type = _get_pycaps_animation(anim_style)
        if animation is not None and element_type is not None:
            builder = builder.add_animation(
                animation,
                EventType.ON_NARRATION_STARTS,
                element_type,
            )

        builder.build().run()

        logger.info("Subtitle rendering complete (Pycaps): {}", output_path.name)
        return output_path

    def _render_ass(
        self,
        video_path: Path,
        output_path: Path,
        segments: list,
        style: dict,
    ) -> Path:
        """
        Burn multi-speaker ASS subtitles onto video using FFmpeg.

        Called automatically by render() when more than one unique speaker
        is detected in the segment list.
        """
        import subprocess

        from utils.ffmpeg_utils import FFMPEG_BIN, FFmpegError

        # Probe native video dimensions so ASS PlayResX/PlayResY match exactly.
        vid_w, vid_h = _get_video_dimensions(video_path)
        logger.info("Video dimensions for ASS script: {}×{}", vid_w, vid_h)

        ass_path = output_path.parent / "subtitles.ass"
        ass_content = _build_ass_content(segments, style, video_width=vid_w, video_height=vid_h)
        ass_path.write_text(ass_content, encoding="utf-8")

        logger.info(
            "Multi-speaker render: burning ASS subtitles via FFmpeg ({} → {})",
            video_path.name,
            output_path.name,
        )

        # FFmpeg subtitles filter requires forward slashes and escaped colons on Windows
        ass_str = str(ass_path).replace("\\", "/").replace(":", "\\:")

        def _build_cmd(use_nvenc: bool) -> list[str]:
            cmd = [FFMPEG_BIN, "-y"]
            if use_nvenc:
                cmd += ["-hwaccel", "cuda"]
            cmd += ["-i", str(video_path)]
            cmd += ["-vf", f"subtitles=\'{ass_str}\'"]
            if use_nvenc:
                # NVENC encoder — p4 = balanced quality/speed, cq 23 ≈ libx264 crf 23
                cmd += ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "23"]
            # Always encode audio to AAC: -c:a copy can silently drop audio when
            # the source codec (e.g. Opus in MKV) is not valid inside MP4.
            cmd += ["-c:a", "aac", "-b:a", "192k"]
            cmd += [str(output_path)]
            return cmd

        # Try NVENC (GPU) first, fall back to CPU libx264 on failure
        for use_nvenc in (True, False):
            encoder_label = "NVENC/GPU" if use_nvenc else "libx264/CPU"
            cmd = _build_cmd(use_nvenc)
            logger.info("FFmpeg encoding with {} encoder", encoder_label)
            logger.debug("FFmpeg ASS cmd: {}", " ".join(cmd))

            result = subprocess.run(cmd, capture_output=True)
            if result.returncode == 0:
                break

            err = result.stderr.decode(errors="replace")
            if use_nvenc:
                logger.warning(
                    "NVENC unavailable or failed — retrying with CPU encoder. "
                    "Reason: {}",
                    err[:300],
                )
            else:
                logger.error("FFmpeg ASS failed: {}", err)
                raise FFmpegError(
                    f"FFmpeg ASS subtitle burn failed (exit {result.returncode}).\n"
                    f"Stderr:\n{err}"
                )

        logger.info("Subtitle rendering complete (ASS/FFmpeg): {}", output_path.name)
        return output_path
