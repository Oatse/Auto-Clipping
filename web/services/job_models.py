"""
web.services.job_models — Job state types shared by route handlers
and the pipeline runner.

Extracted from ``web/server.py`` so the runner module can import these
without dragging the entire FastAPI app along.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# Phase number → human-readable label.  Kept as module-level constant so
# both the pipeline runner and the route handlers refer to the same map.
PHASE_LABELS: dict[int, str] = {
    1: "Transcription & Diarization",
    2: "Translation",
    3: "Subtitle Rendering",
    4: "Final Muxing",
}


class Job(BaseModel):
    id: str
    filename: str
    target_language: str
    status: JobStatus
    progress_pct: float = 0.0
    current_phase: int = 0
    phase_label: str | None = None
    output_file: str | None = None
    error: str | None = None
    created_at: float
    started_at: float | None = None
    completed_at: float | None = None
    log_lines: list[str] = []
    video_path: str | None = None          # Path video yang diupload
    transcript_path: str | None = None     # Path file JSON transkripsi hasil phase 1
    transcribe_only: bool = False          # Jika True, pipeline berhenti setelah phase 1
    num_speakers: int | None = None        # Manual speaker count override (None = auto)
    speaker_detection: bool = True         # False = skip gap detection, semua SPEAKER_00

    class Config:
        use_enum_values = True
