from __future__ import annotations

from pathlib import Path

from models.transcript import TranscriptSegment, WordTimestamp
from processors.subtitle_renderer import _build_ass_content
from web.services.job_models import Job, JobStatus
from web.services.pipeline_runner import (
    _apply_render_caption_style,
    _normalize_segment_visuals,
    _unique_render_output_path,
)


def _job(*, natural_caption: bool = True) -> Job:
    return Job(
        id="abc123def456",
        filename="source.mp4",
        target_language="en",
        status=JobStatus.COMPLETED,
        created_at=1.0,
        natural_caption=natural_caption,
    )


def _segment() -> TranscriptSegment:
    return TranscriptSegment(
        start=0.0,
        end=4.0,
        text="this is a manually merged subtitle sentence.",
        speaker="SPEAKER_00",
        words=[
            WordTimestamp(word="this", start=0.0, end=0.5),
            WordTimestamp(word="is", start=0.5, end=1.0),
            WordTimestamp(word="a", start=1.0, end=1.5),
            WordTimestamp(word="manually", start=1.5, end=2.0),
            WordTimestamp(word="merged", start=2.0, end=2.5),
            WordTimestamp(word="subtitle", start=2.5, end=3.0),
            WordTimestamp(word="sentence.", start=3.0, end=4.0),
        ],
    )


def _long_segment() -> TranscriptSegment:
    return TranscriptSegment(
        start=0.0,
        end=4.0,
        text="HE JUST BEEN YOU KNOW THOSE NAIL CLIPPER THINGS?",
        speaker="SPEAKER_00",
        words=[
            WordTimestamp(word="HE", start=0.0, end=0.3),
            WordTimestamp(word="JUST", start=0.3, end=0.7),
            WordTimestamp(word="BEEN", start=0.7, end=1.0),
            WordTimestamp(word="YOU", start=1.0, end=1.3),
            WordTimestamp(word="KNOW", start=1.3, end=1.7),
            WordTimestamp(word="THOSE", start=1.7, end=2.1),
            WordTimestamp(word="NAIL", start=2.1, end=2.5),
            WordTimestamp(word="CLIPPER", start=2.5, end=3.2),
            WordTimestamp(word="THINGS?", start=3.2, end=4.0),
        ],
    )


def _short_segment() -> TranscriptSegment:
    return TranscriptSegment(
        start=0.0,
        end=1.0,
        text="short caption",
        speaker="SPEAKER_00",
        words=[
            WordTimestamp(word="short", start=0.0, end=0.5),
            WordTimestamp(word="caption", start=0.5, end=1.0),
        ],
    )


def test_unique_render_output_path_when_first_render_exists(tmp_path: Path) -> None:
    existing = tmp_path / "source_subtitled_en.mp4"
    existing.write_bytes(b"old render")

    result = _unique_render_output_path(tmp_path, "source", "en")

    assert result == tmp_path / "source_subtitled_en_r2.mp4"


def test_unique_render_output_path_when_multiple_rerenders_exist(
    tmp_path: Path,
) -> None:
    for name in (
        "source_subtitled_en.mp4",
        "source_subtitled_en_r2.mp4",
        "source_subtitled_en_r3.mp4",
    ):
        (tmp_path / name).write_bytes(b"old render")

    result = _unique_render_output_path(tmp_path, "source", "en")

    assert result == tmp_path / "source_subtitled_en_r4.mp4"


def test_caption_style_keeps_manual_preview_merge_as_one_segment() -> None:
    result = _apply_render_caption_style(
        [_segment()],
        job=_job(),
        has_user_transcript=True,
    )

    assert len(result) == 1
    assert result[0].text == "this is a manually merged subtitle sentence"


def test_caption_style_splits_cached_non_preview_transcript() -> None:
    result = _apply_render_caption_style(
        [_segment()],
        job=_job(),
        has_user_transcript=False,
    )

    assert len(result) > 1


def test_visual_normalization_clamps_position_and_drops_invalid_effect() -> None:
    segment = TranscriptSegment(
        start=0.0,
        end=1.0,
        text="caption",
        speaker="SPEAKER_00",
        pos_x=140.0,
        pos_y=-10.0,
        pos_override=True,
        effect={"type": "invalid", "strength": "expert"},
    )

    result = _normalize_segment_visuals(segment)

    assert result.pos_x == 100.0
    assert result.pos_y == 0.0
    assert result.pos_override is True
    assert result.effect is None


def test_subtitle_renderer_uses_higher_bottom_margin() -> None:
    ass_content = _build_ass_content(
        [_segment()],
        {},
        video_width=1920,
        video_height=1080,
    )

    assert ",10,10,43,1" in ass_content
    assert ",0,0,43,," in ass_content


def test_subtitle_renderer_animation_does_not_change_layout_scale() -> None:
    ass_content = _build_ass_content(
        [_segment()],
        {"animStyle": "word-pop"},
        video_width=1920,
        video_height=1080,
    )

    assert "\\fad(" in ass_content
    assert "\\fscx" not in ass_content
    assert "\\fscy" not in ass_content


def test_subtitle_renderer_breaks_long_text_into_two_lines() -> None:
    ass_content = _build_ass_content(
        [_long_segment()],
        {"animStyle": "word-pop"},
        video_width=1920,
        video_height=1080,
    )

    assert "HE JUST BEEN YOU KNOW\\NTHOSE NAIL CLIPPER THINGS?" in ass_content


def test_subtitle_renderer_keeps_short_text_on_one_line() -> None:
    ass_content = _build_ass_content(
        [_short_segment()],
        {"animStyle": "word-pop"},
        video_width=1920,
        video_height=1080,
    )

    assert "\\N" not in ass_content
