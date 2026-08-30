from __future__ import annotations

from models.transcript import TranscriptSegment
from processors.subtitle_renderer import _build_ass_content


def test_ass_uses_custom_segment_position() -> None:
    segment = TranscriptSegment(
        start=0.0,
        end=1.0,
        text="custom position",
        speaker="SPEAKER_00",
        pos_x=25.0,
        pos_y=75.0,
        pos_override=True,
    )

    result = _build_ass_content(
        [segment],
        {"position": "bottom"},
        video_width=1000,
        video_height=800,
    )

    assert r"{\fad(80,100)}{\an5\pos(250,600)}" in result


def test_ass_samples_only_effect_segment() -> None:
    affected = TranscriptSegment(
        start=0.0,
        end=1.0,
        text="moves",
        speaker="SPEAKER_00",
        effect={"type": "wave", "axis": "horizontal", "strength": "medium"},
    )
    unaffected = TranscriptSegment(
        start=1.0,
        end=2.0,
        text="stays",
        speaker="SPEAKER_00",
    )

    result = _build_ass_content(
        [affected, unaffected],
        {"position": "bottom"},
        video_width=1000,
        video_height=800,
        video_fps=10.0,
    )

    assert result.count("Dialogue:") == 11
    assert result.count("moves") == 10
    assert result.count("stays") == 1
    assert r"{\an5\pos(" in result


def test_ass_effect_frames_do_not_restart_fade_animation() -> None:
    segment = TranscriptSegment(
        start=0.0,
        end=1.0,
        text="shake without flicker",
        speaker="SPEAKER_00",
        effect={"type": "shake", "strength": "medium"},
    )

    result = _build_ass_content(
        [segment],
        {"position": "bottom", "animStyle": "word-pop"},
        video_width=1000,
        video_height=800,
        video_fps=30.0,
    )

    effect_lines = [
        line for line in result.splitlines()
        if line.startswith("Dialogue:") and "shake without flicker" in line
    ]

    assert len(effect_lines) == 30
    assert all(r"\fad(" not in line for line in effect_lines)
