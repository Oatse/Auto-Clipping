from __future__ import annotations

import asyncio
import json

import pytest

from models.transcript import TranscriptSegment
from processors.subtitle_effects import effect_offset, normalize_segment_effect
from web.routes.auto_subtitle import get_transcript
from web.services import job_state
from web.services.job_models import Job, JobStatus


def test_normalize_segment_effect_accepts_wave_axis_and_strength() -> None:
    result = normalize_segment_effect(
        {"type": "wave", "axis": "horizontal", "strength": "medium"},
    )

    assert result == {
        "type": "wave",
        "axis": "horizontal",
        "strength": "medium",
    }


@pytest.mark.parametrize(
    "raw",
    [
        None,
        {},
        {"type": "unknown"},
        {"type": "wave", "axis": "diagonal"},
        {"type": "shake", "strength": "maximum"},
    ],
)
def test_normalize_segment_effect_rejects_invalid_values(raw: dict | None) -> None:
    assert normalize_segment_effect(raw) is None


def test_effect_offset_is_zero_outside_segment() -> None:
    effect = {"type": "wave", "axis": "vertical", "strength": "soft"}

    assert effect_offset(effect, local_time=-0.1, duration=2.0) == (0.0, 0.0)
    assert effect_offset(effect, local_time=2.1, duration=2.0) == (0.0, 0.0)


def test_effect_offset_is_axis_constrained_and_deterministic() -> None:
    horizontal = {"type": "wave", "axis": "horizontal", "strength": "medium"}

    first = effect_offset(horizontal, local_time=0.25, duration=2.0)
    second = effect_offset(horizontal, local_time=0.25, duration=2.0)

    assert first == second
    assert first[0] != 0.0
    assert first[1] == 0.0


def test_shake_strength_increases_displacement_budget() -> None:
    budgets = []
    for strength in ("soft", "medium", "expert"):
        offsets = [
            effect_offset(
                {"type": "shake", "strength": strength},
                local_time=sample / 100.0,
                duration=2.0,
            )
            for sample in range(100)
        ]
        budgets.append(max(sum(abs(value) for value in offset) for offset in offsets))

    assert budgets[0] < budgets[1]
    assert budgets[1] < budgets[2]


def test_transcript_segment_round_trip_preserves_effect_and_position() -> None:
    segment = TranscriptSegment(
        start=1.0,
        end=3.0,
        text="hello",
        speaker="SPEAKER_00",
        pos_x=32.5,
        pos_y=64.0,
        pos_override=True,
        effect={"type": "shake", "strength": "expert"},
    )

    restored = TranscriptSegment.from_dict(segment.to_dict())

    assert restored.pos_x == 32.5
    assert restored.pos_y == 64.0
    assert restored.pos_override is True
    assert restored.effect == {"type": "shake", "strength": "expert"}


def test_get_transcript_restores_saved_segment_effect(
    tmp_path,
    monkeypatch,
) -> None:
    transcript_path = tmp_path / "source_transcript.json"
    transcript_path.write_text(
        json.dumps(
            {
                "segments": [
                    {
                        "start": 1.0,
                        "end": 3.0,
                        "text": "hello",
                        "speaker": "SPEAKER_00",
                        "effect": {
                            "type": "wave",
                            "axis": "vertical",
                            "strength": "medium",
                        },
                    },
                ],
            },
        ),
        encoding="utf-8",
    )
    job_id = "effect-reload-test"
    monkeypatch.setitem(
        job_state.jobs,
        job_id,
        Job(
            id=job_id,
            filename="fixture.mp4",
            target_language="en",
            status=JobStatus.COMPLETED,
            created_at=0.0,
            transcript_path=str(transcript_path),
        ),
    )

    response = asyncio.run(get_transcript(job_id))

    assert response["segments"][0]["effect"] == {
        "type": "wave",
        "axis": "vertical",
        "strength": "medium",
    }
