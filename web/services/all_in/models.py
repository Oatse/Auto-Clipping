"""
web.services.all_in.models — Job state types for the All In Workspace.

Mirrors the shape of ``web.services.job_models`` but adds per-Clip
status tracking so the UI can stream Clip Cards as they finish and
expose a per-clip retry button (see ADR-0002 + design grilling Q10).

The ``AllInJob`` is persisted to disk as ``job_meta.json`` next to
its source video and finished clips, so it survives server restarts.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


# ─── Status enums ─────────────────────────────────────────────────────────────

class AllInJobStatus(str, Enum):
    """Top-level Job status.

    ``failed`` only fires when a non-recoverable Job-level step dies
    (source download, Gemini analysis returned nothing).  A Job with
    some failed Clips and some finished Clips is still ``completed``.
    """

    QUEUED = "queued"
    DOWNLOADING = "downloading"      # fetching source video
    ANALYZING = "analyzing"          # transcript + signals + Gemini
    RENDERING = "rendering"          # per-Clip loop in flight
    COMPLETED = "completed"          # every Clip reached a terminal state
    FAILED = "failed"                # Job-level fatal error
    CANCELLED = "cancelled"


class AllInClipStatus(str, Enum):
    """Per-Clip status inside an All In Job.

    Each Clip moves: pending → rendering → done | failed.  Failed
    Clips stay on the Job and can be retried via the per-clip retry
    endpoint without re-running the expensive download/analyze stages.
    """

    PENDING = "pending"
    RENDERING = "rendering"
    DONE = "done"
    FAILED = "failed"


# ─── Aspect ratio ─────────────────────────────────────────────────────────────

class AspectRatio(str, Enum):
    """Output aspect ratio for the reframe stage.

    ORIGINAL skips reframe entirely — output is the cut+captioned clip
    at the source video's native aspect.  SHORT_9_16 and SQUARE_1_1
    use the smart-static face-crop strategy with centre-crop fallback
    (see design grilling Q5).
    """

    ORIGINAL = "original"
    SHORT_9_16 = "9:16"
    SQUARE_1_1 = "1:1"


# ─── Caption preset ───────────────────────────────────────────────────────────

class CaptionPreset(str, Enum):
    """Named subtitle style preset (see ``presets.py`` for definitions)."""

    BOLD = "bold"
    MINIMAL = "minimal"
    KARAOKE = "karaoke"


# ─── Detection mode (mirrors Clip Finder) ─────────────────────────────────────

class DetectionMode(str, Enum):
    SINGLE_SHOT = "single-shot"
    MULTI_STAGE = "multi-stage"


# ─── Per-Clip state ───────────────────────────────────────────────────────────

class AllInClip(BaseModel):
    """One Clip inside an All In Job.

    Wraps the underlying Moment data (from Clip Finder) plus per-Clip
    pipeline state.  ``clip_file`` is set once the Clip reaches
    ``DONE``; ``error`` is set once it reaches ``FAILED``.
    """

    # Moment data (mirrors the Moment.to_dict() shape from clip_finder)
    index: int                          # ordinal in the source video
    start: float                        # source-video start (seconds)
    end: float                          # source-video end (seconds)
    title: str                          # Gemini-generated title
    reason: str                         # Gemini-generated brief description
    score: float                        # 0.0–10.0, drives sort + colour band
    highlight_type: str | None = None   # passthrough from Moment.score
    hunter: str | None = None           # passthrough from Moment.score

    # Pipeline state
    status: AllInClipStatus = AllInClipStatus.PENDING
    stage_label: str | None = None      # human-readable current stage
    clip_file: str | None = None        # path to finished MP4 (when DONE)
    error: str | None = None            # error message (when FAILED)

    class Config:
        use_enum_values = True


# ─── All In Job ───────────────────────────────────────────────────────────────

class AllInJob(BaseModel):
    """Top-level All In Workspace Job.

    Owns the source video on disk for the lifetime of the Job (per
    design grilling Q12).  Per-Clip state lives on ``clips[i]`` so the
    UI can stream Clip Cards as they finish and offer per-clip retry.
    """

    # Identity
    id: str
    created_at: float

    # Inputs (mirrors the form on the All In page)
    url: str
    instructions: str = ""
    analysis_lang: str = "en"           # transcript language for Gemini analysis
    caption_lang: str = "en"            # burned-in subtitle language

    # Refinement settings
    aspect_ratio: AspectRatio = AspectRatio.SHORT_9_16
    tighten_silence: bool = True        # silence-trim toggle, default ON (Q6)
    speaker_tinting: bool = False       # caption colour-by-speaker, default OFF (Q4)
    auto_subtitle: bool = True          # burn captions, default ON (Q7)
    caption_preset: CaptionPreset = CaptionPreset.BOLD

    # Advanced (mirrors Clip Finder, hidden behind <details> disclosure)
    mode: DetectionMode = DetectionMode.SINGLE_SHOT
    enable_audio_signals: bool = True
    enable_chat_signals: bool = True
    start_offset: float = 0.0
    max_clips: int = 12

    # Job-level pipeline state
    status: AllInJobStatus = AllInJobStatus.QUEUED
    progress_pct: float = 0.0
    phase_label: str = "Queued"
    error: str | None = None
    completed_at: float | None = None

    # Source video (kept for the lifetime of the Job — see Q12)
    source_path: str | None = None
    source_title: str | None = None
    transcript: list[dict] = Field(default_factory=list)
    signals_summary: dict = Field(default_factory=dict)

    # Per-Clip state (streamed to the UI as Clip Cards)
    clips: list[AllInClip] = Field(default_factory=list)

    # Live log lines (SSE)
    log_lines: list[str] = Field(default_factory=list)

    class Config:
        use_enum_values = True

    # ── Helpers ────────────────────────────────────────────────────────────

    def is_terminal(self) -> bool:
        """True once every Clip has reached DONE or FAILED."""
        if not self.clips:
            return False
        terminal = {AllInClipStatus.DONE.value, AllInClipStatus.FAILED.value}
        return all(c.status in terminal for c in self.clips)

    def done_count(self) -> int:
        return sum(1 for c in self.clips if c.status == AllInClipStatus.DONE.value)

    def failed_count(self) -> int:
        return sum(1 for c in self.clips if c.status == AllInClipStatus.FAILED.value)
