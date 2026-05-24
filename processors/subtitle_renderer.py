"""
processors/subtitle_renderer.py — Phase 3: Pycaps Subtitle Injection.

Responsibilities:
  1. Build a Pycaps-compatible word-level JSON from ElevenLabs word timestamps
     + global video segment offsets.
  2. Bypass Pycaps internal transcription entirely.
  3. Apply CSS styling derived from the UI style_config dict.
  4. Single-speaker: render via Pycaps CapsPipelineBuilder.
  5. Multi-speaker: generate ASS subtitle file + burn via FFmpeg
     (ASS natively supports simultaneous multi-line subtitles + per-speaker colors).

Formula:
    Global_Word_Start = Segment_Start_Time_In_Video + Word_Start_Time
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

    # Build text-shadow: extra strokes (outermost first) + primary stroke + glow
    shadows = []

    # Extra strokes — outermost (widest) first, then progressively smaller
    extra_strokes = style.get("extraStrokes", []) or []
    if stroke_enabled and extra_strokes:
        sorted_extra = sorted(extra_strokes, key=lambda s: s.get("width", 0), reverse=True)
        for es in sorted_extra:
            ew = int(es.get("width", 0))
            ec = es.get("color", "#000000")
            if ew > 0:
                shadows += [
                    f"{ew}px {ew}px 0 {ec}",
                    f"-{ew}px {ew}px 0 {ec}",
                    f"{ew}px -{ew}px 0 {ec}",
                    f"-{ew}px -{ew}px 0 {ec}",
                    f"{ew}px 0 0 {ec}",
                    f"-{ew}px 0 0 {ec}",
                    f"0 {ew}px 0 {ec}",
                    f"0 -{ew}px 0 {ec}",
                ]

    # Primary stroke
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
    """Convert seconds to ASS timestamp h:mm:ss.cc

    Rounds to the nearest centisecond first, then derives h/m/s/cs from
    the integer total so that carry-over (e.g. 99.5 cs → +1 s) is handled
    correctly.  Previously, rounding was applied after splitting, which
    could produce invalid centisecond values like '100'.
    """
    total_cs = int(round(secs * 100))
    cs = total_cs % 100
    total_s = total_cs // 100
    s = total_s % 60
    total_m = total_s // 60
    m = total_m % 60
    h = total_m // 60
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
    #
    # IMPORTANT: trim only ``all_fields`` (the local tuple list) — never
    # mutate the caller's segment objects.  This renderer is called from
    # the pipeline runner with the live ``translated_segments`` list, and
    # downstream phases (mux, AE export, preview re-render) re-read those
    # objects.  Mutating them here would change end-times the user can see.
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
            nxt_start, _nxt_end, _ = all_fields[nxt_idx]
            if cur_end > nxt_start:
                # Shrink toward the next segment but never past its start.
                # min() picks the smaller of (nxt_start - 0.01) and the
                # original end, then max() ensures we don't go below
                # cur_start + 0.05 (degenerate near-zero duration).
                new_end = max(cur_start + 0.05, min(cur_end, nxt_start - 0.01))
                all_fields[cur_idx] = (cur_start, new_end, _sp)
                logger.debug(
                    "ASS: trimmed same-speaker overlap for {} seg#{}: "
                    "end {:.3f} -> {:.3f}",
                    _sp, cur_idx, cur_end, new_end,
                )

    # Build dialogue lines with dynamic per-line MarginV
    # Extra strokes: duplicate each line at lower ASS layers with \bord and \3c overrides
    extra_strokes = style.get("extraStrokes", []) or []
    if stroke_enabled and extra_strokes:
        sorted_extra = sorted(extra_strokes, key=lambda s: s.get("width", 0), reverse=True)
    else:
        sorted_extra = []
    # Layer offset: extra strokes use layers 0..N-1, primary text uses layer N
    base_layer = len(sorted_extra)

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

        # ASS escapes: curly braces are control codes — must be escaped as \{ \}
        # Use raw strings so the literal output is "\{" / "\}" (single backslash + brace).
        text_esc = text.replace("{", r"\{").replace("}", r"\}")
        anim_tag = _get_ass_anim_tag(style.get("animStyle", "word-pop"))

        # Extra stroke layers (rendered behind primary text)
        for es_idx, es in enumerate(sorted_extra):
            es_w = int(es.get("width", 0))
            es_c_hex = es.get("color", "#000000")
            es_r, es_g, es_b = hex_to_rgb(es_c_hex)
            es_ass_color = _rgb_to_ass(es_r, es_g, es_b)
            # Override outline: \3c = outline color, \bord = outline width
            # \4a&HFF = fully transparent shadow for extra stroke layers
            override = r"{\3c" + es_ass_color + r"\bord" + str(es_w) + r"\4a&HFF&\shad0}"
            dialogue_lines.append(
                f"Dialogue: {es_idx},{_secs_to_ass_time(start)},{_secs_to_ass_time(end)},"
                f"{sp},,0,0,{line_margin_v},,{anim_tag}{override}{text_esc}"
            )

        # Primary text layer (topmost)
        dialogue_lines.append(
            f"Dialogue: {base_layer},{_secs_to_ass_time(start)},{_secs_to_ass_time(end)},"
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
    word-level JSON derived from ElevenLabs word alignment data.
    """

    def build_pycaps_transcript(
        self,
        segments: list[TranscriptSegment],
        output_dir: Path | str,
    ) -> Path:
        """
        Build and save the Pycaps-compatible word-level transcript JSON.

        For each word in each segment:
            global_start = segment.start + word.start
            global_end   = segment.start + word.end

        Parameters
        ----------
        segments:
            Transcript segments with ElevenLabs word timestamps.
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

        # All render paths route through ASS/FFmpeg.  The legacy Pycaps
        # ``CapsPipelineBuilder`` path was retired (see git history) — it
        # only ever ran for single-speaker, no-segments inputs and the
        # pipeline runner now always passes ``segments``, so the branch
        # was dead code that imported a heavy optional dependency at
        # render-time.  ``pycaps_transcript`` (the JSON the renderer used
        # to feed Pycaps) is still built upstream because the file is
        # useful as a debug artifact and is referenced by the After
        # Effects export, but it's no longer consumed by the renderer.
        return self._render_ass(video_path, output_path, segs, style)

    def _render_ass(
        self,
        video_path: Path,
        output_path: Path,
        segments: list,
        style: dict,
    ) -> Path:
        """
        Burn multi-speaker ASS subtitles onto video using FFmpeg.

        Also applies timeline effects (red-flash, zoom, shake, vignette, etc.)
        and global color filters (brightness/contrast/saturation) when present
        in the style_config dict.

        Called automatically by render() when segments are available.
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

        # ── Extract effects & color filter from style config ─────────────
        effects = style.get("effects", []) or []
        color_filter = style.get("filter", {}) or {}

        # ── Build video filter chain ─────────────────────────────────────
        vf_parts: list[str] = []

        # 1. Subtitles — always first so text is burned before overlays
        vf_parts.append(f"subtitles='{ass_str}'")

        # 2. Per-effect visual overlays (drawbox, vignette)
        for fx in effects:
            fx_type = fx.get("type", "")
            start = float(fx.get("start", 0))
            end = float(fx.get("end", 0))
            params = fx.get("params", {}) or {}
            enable = f"between(t\\,{start:.3f}\\,{end:.3f})"

            if fx_type == "red-flash":
                alpha = (params.get("intensity", 60) / 100) * 0.6
                vf_parts.append(
                    f"drawbox=x=0:y=0:w=iw:h=ih:color=red@{alpha:.2f}:t=fill:enable='{enable}'"
                )
            elif fx_type == "flash-white":
                alpha = (params.get("intensity", 70) / 100) * 0.75
                vf_parts.append(
                    f"drawbox=x=0:y=0:w=iw:h=ih:color=white@{alpha:.2f}:t=fill:enable='{enable}'"
                )
            elif fx_type == "vignette":
                # Intensity 10-100 → vignette angle PI/6..PI/2
                intensity = params.get("intensity", 60)
                angle = 0.52 + (intensity / 100) * 1.05  # ~PI/6 to ~PI/2
                vf_parts.append(f"vignette={angle:.2f}:enable='{enable}'")

        # 3. Zoom effects (zoom-in-center, zoom-vtuber)
        #    Uses per-effect zoomLevel and cropX/cropY (center point) params.
        zoom_effects = [
            fx for fx in effects
            if fx.get("type") in ("zoom-in-center", "zoom-vtuber")
        ]
        if zoom_effects:
            for fx in zoom_effects:
                start = float(fx.get("start", 0))
                end = float(fx.get("end", 0))
                params = fx.get("params", {}) or {}
                enable = f"between(t\\,{start:.3f}\\,{end:.3f})"
                zoom_level = params.get("zoomLevel", 130)
                scale_factor = zoom_level / 100.0
                vf_parts.append(
                    f"scale=w=iw*{scale_factor:.2f}:h=ih*{scale_factor:.2f}:enable='{enable}'"
                )
            # Crop back to native dimensions using cropX/cropY offsets
            # Collect all crop center expressions so multiple zooms merge
            crop_x_parts = []
            crop_y_parts = []
            for fx in zoom_effects:
                start = float(fx.get("start", 0))
                end = float(fx.get("end", 0))
                params = fx.get("params", {}) or {}
                en = f"between(t\\,{start:.3f}\\,{end:.3f})"
                cx = params.get("cropX", 0.5)
                cy = params.get("cropY", 0.5)
                zoom_level = params.get("zoomLevel", 130)
                sf = zoom_level / 100.0
                # When zoom is active: offset from center based on cropX/cropY
                x_off = (cx - 0.5) * vid_w * (sf - 1)
                y_off = (cy - 0.5) * vid_h * (sf - 1)
                crop_x_parts.append(f"{x_off:.1f}*{en}")
                crop_y_parts.append(f"{y_off:.1f}*{en}")

            cx_expr = "+".join(crop_x_parts) if crop_x_parts else "0"
            cy_expr = "+".join(crop_y_parts) if crop_y_parts else "0"
            vf_parts.append(
                f"crop={vid_w}:{vid_h}:"
                f"(in_w-{vid_w})/2+{cx_expr}:"
                f"(in_h-{vid_h})/2+{cy_expr}"
            )

        # 4. Shake effects — per-effect intensity controls displacement
        shake_effects = [
            fx for fx in effects if fx.get("type") == "shake"
        ]
        if shake_effects:
            pad = 20  # pixels of room on each side
            vf_parts.append(
                f"scale={vid_w + pad * 2}:{vid_h + pad * 2}"
            )
            sx_parts = []
            sy_parts = []
            for fx in shake_effects:
                start = float(fx.get("start", 0))
                end = float(fx.get("end", 0))
                params = fx.get("params", {}) or {}
                en = f"between(t\\,{start:.3f}\\,{end:.3f})"
                intensity = params.get("intensity", 50)
                amp = 4 + (intensity / 100) * 12  # 4-16 pixels displacement
                sx_parts.append(f"{amp:.0f}*sin(t*45)*{en}")
                sy_parts.append(f"{amp:.0f}*cos(t*37)*{en}")
            sx_expr = "+".join(sx_parts)
            sy_expr = "+".join(sy_parts)
            vf_parts.append(
                f"crop={vid_w}:{vid_h}:"
                f"(in_w-{vid_w})/2+{sx_expr}:"
                f"(in_h-{vid_h})/2+{sy_expr}"
            )

        # 5. Global color grading (eq filter)
        filter_name = color_filter.get("name", "none")
        br = float(color_filter.get("brightness", 0))
        co = float(color_filter.get("contrast", 0))
        sa = float(color_filter.get("saturation", 0))
        has_eq = filter_name != "none" or br != 0 or co != 0 or sa != 0

        if has_eq:
            # FFmpeg eq: brightness [-1,1], contrast centered at 1, saturation centered at 1
            eq_b = br / 100.0        # UI  -50..50  → FFmpeg -0.5..0.5
            eq_c = 1.0 + co / 100.0  # UI  -50..50  → FFmpeg  0.5..1.5
            eq_s = 1.0 + sa / 100.0  # UI  -50..50  → FFmpeg  0.5..1.5
            vf_parts.append(
                f"eq=brightness={eq_b:.4f}:contrast={eq_c:.4f}:saturation={eq_s:.4f}"
            )

        vf_str = ",".join(vf_parts)

        # ── Build audio filter chain ─────────────────────────────────────
        af_parts: list[str] = []
        for fx in effects:
            fx_type = fx.get("type", "")
            start = float(fx.get("start", 0))
            end = float(fx.get("end", 0))
            params = fx.get("params", {}) or {}
            enable = f"between(t\\,{start:.3f}\\,{end:.3f})"

            if fx_type == "volume-boost":
                gain = params.get("gain", 2.0)
                af_parts.append(f"volume={gain:.1f}:enable='{enable}'")
            elif fx_type == "bass-boost":
                gain = params.get("gain", 6)
                af_parts.append(
                    f"equalizer=f=80:t=h:w=200:g={gain:.1f}:enable='{enable}'"
                )
        af_str = ",".join(af_parts) if af_parts else None

        # ── Log effects info ─────────────────────────────────────────────
        if effects:
            logger.info("Applying {} timeline effect(s) to render", len(effects))
            for fx in effects:
                logger.debug(
                    "  FX: {} @ {:.2f}s–{:.2f}s params={}",
                    fx.get("type"),
                    float(fx.get("start", 0)),
                    float(fx.get("end", 0)),
                    fx.get("params", {}),
                )
        if has_eq:
            logger.info(
                "Applying color filter '{}': brightness={}, contrast={}, saturation={}",
                filter_name, br, co, sa,
            )

        def _build_cmd(use_nvenc: bool) -> list[str]:
            cmd = [FFMPEG_BIN, "-y"]
            if use_nvenc:
                cmd += ["-hwaccel", "cuda"]
            cmd += ["-i", str(video_path)]
            cmd += ["-vf", vf_str]
            if af_str:
                cmd += ["-af", af_str]
            if use_nvenc:
                # NVENC encoder — p4 = balanced quality/speed, cq 23 ≈ libx264 crf 23
                cmd += ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "23"]
            # Always encode audio to AAC: -c:a copy can silently drop audio when
            # the source codec (e.g. Opus in MKV) is not valid inside MP4.
            cmd += ["-c:a", "aac", "-b:a", "192k"]
            # Strip any embedded subtitle streams from the source video so
            # they don't leak through (briefly showing original-language text).
            cmd += ["-sn"]
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
