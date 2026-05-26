"""
web/routes/auto_subtitle.py — Workspace 01 (Auto-Subtitle) HTTP surface.

The biggest workspace by surface area: ~14 endpoints driving the
upload / transcribe / preview-edit / render / export pipeline.

Endpoints:
  - GET    /api/jobs                            list all jobs
  - GET    /api/jobs/{id}                       poll one job
  - DELETE /api/jobs/{id}                       cancel + clean up
  - GET    /api/download/{id}                   final captioned MP4
  - POST   /api/jobs                            create job from upload
  - GET    /api/jobs/{id}/transcript            Phase 1 transcript
  - PUT    /api/jobs/{id}/transcript            save user edits
  - GET    /api/jobs/{id}/transcript/original   raw ElevenLabs words
  - GET    /api/jobs/{id}/video                 stream original video
  - POST   /api/jobs/{id}/render                kick Phase 2-4 with style
  - POST   /api/jobs/{id}/export-ae             After Effects .jsx export
  - GET    /api/jobs/{id}/log                   SSE log stream
  - POST   /api/jobs/from-clip                  derive a Job from a clip-finder file

State is shared via ``web.services.job_state``
(``jobs`` / ``job_tasks``).

Mounted by ``web/server.py``::

    from web.routes.auto_subtitle import (
        router as auto_subtitle_router,
        register_restore_hook as _register_jobs_restore,
    )
    app.include_router(auto_subtitle_router)
    _register_jobs_restore(app)
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import AsyncGenerator

from fastapi import (
    APIRouter,
    BackgroundTasks,
    FastAPI,
    File,
    Form,
    HTTPException,
    UploadFile,
)
from fastapi.responses import FileResponse, StreamingResponse
from loguru import logger
from pydantic import BaseModel

import config

from web.services import job_state
from web.services.job_models import Job, JobStatus
from web.services.upload_helpers import (
    UploadTooLargeError,
    safe_upload_name,
    save_upload_streaming,
)


router = APIRouter()


# ─── Request schemas ─────────────────────────────────────────────────────────


class TranscriptUpdateRequest(BaseModel):
    segments: list[dict]


class RenderRequest(BaseModel):
    style_config: dict = {}


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _is_job_id(name: str) -> bool:
    """Return True if ``name`` looks like a 12-char hex job ID."""
    return bool(re.match(r"^[0-9a-f]{12}$", name))


def _get_job_or_404(job_id: str) -> Job:
    """Lookup a Job by ID or raise 404."""
    job = job_state.jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    return job


# ─── Restore hook ────────────────────────────────────────────────────────────


async def _restore_jobs_from_disk() -> None:
    """Scan the output/ directory on startup and restore jobs that have a
    saved transcript so they appear in the Recent Jobs list."""
    if not job_state.OUTPUT_ROOT.exists():
        return

    restored = 0
    for job_dir in sorted(
        job_state.OUTPUT_ROOT.iterdir(),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    ):
        if not job_dir.is_dir():
            continue
        job_id = job_dir.name
        if not _is_job_id(job_id):
            continue
        if job_id in job_state.jobs:
            continue  # already tracked in memory

        transcript_file = job_dir / "phase1_transcription" / "source_transcript.json"
        if not transcript_file.exists():
            continue

        meta_file = job_dir / "job_meta.json"
        try:
            if meta_file.exists():
                with meta_file.open("r", encoding="utf-8") as f:
                    meta = json.load(f)
                # Only keep keys that exist in the Job model.
                valid_keys = set(Job.model_fields.keys())
                filtered = {k: v for k, v in meta.items() if k in valid_keys}
                job = Job(**filtered)
            else:
                # Reconstruct minimal metadata from filesystem.
                video_path = None
                filename = "video.mp4"
                for upload in job_state.UPLOADS_DIR.glob(f"{job_id}_*"):
                    video_path = str(upload)
                    filename = upload.name[len(job_id) + 1:]
                    break

                mtime = transcript_file.stat().st_mtime
                job = Job(
                    id=job_id,
                    filename=filename,
                    target_language="en",
                    status=JobStatus.COMPLETED,
                    current_phase=1,
                    progress_pct=25.0,
                    phase_label="Transcription complete — Ready for preview",
                    created_at=mtime,
                    completed_at=mtime,
                    video_path=video_path,
                    transcript_path=str(transcript_file),
                    transcribe_only=True,
                )

            job_state.jobs[job_id] = job
            restored += 1
        except Exception as exc:  # noqa: BLE001 — never crash startup.
            logger.warning("Could not restore job {}: {}", job_id, exc)

    if restored:
        logger.info("Restored {} job(s) from output directory", restored)


def register_restore_hook(app: FastAPI) -> None:
    """Wire the startup restore hook into ``app``."""
    app.add_event_handler("startup", _restore_jobs_from_disk)


# ─── /api/jobs (list / get / delete) ─────────────────────────────────────────


@router.get("/api/jobs")
async def list_jobs() -> list[dict]:
    jobs = sorted(
        job_state.jobs.values(),
        key=lambda j: j.created_at,
        reverse=True,
    )
    result: list[dict] = []
    for j in jobs:
        d = j.model_dump(exclude={"log_lines", "video_path"})
        d["has_transcript"] = bool(
            j.transcript_path and Path(j.transcript_path).exists()
        )
        result.append(d)
    return result


@router.get("/api/jobs/{job_id}")
async def get_job(job_id: str) -> dict:
    job = _get_job_or_404(job_id)
    return job.model_dump()


@router.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str) -> dict:
    """Cancel an in-flight Job and reclaim its disk bytes."""
    job = _get_job_or_404(job_id)
    task = job_state.job_tasks.pop(job_id, None)
    if task and not task.done():
        task.cancel()
        # Wait briefly so we don't leave a half-written transcript / output
        # file behind. asyncio.shield prevents CancelledError inside the task
        # from bubbling up to the HTTP handler as a 500.
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            pass

    # ── Disk cleanup ────────────────────────────────────────────────────
    output_dir = job_state.OUTPUT_ROOT / job_id
    if output_dir.exists() and output_dir.is_dir():
        try:
            shutil.rmtree(output_dir)
            logger.info("[Job {}] removed output dir {}", job_id[:8], output_dir)
        except Exception as exc:  # noqa: BLE001 — best-effort cleanup.
            logger.warning(
                "[Job {}] could not remove {}: {}", job_id[:8], output_dir, exc,
            )

    # Only delete the source file if it lives under UPLOADS_DIR — jobs
    # created via /api/jobs/from-clip point at a clip-finder MP4 we
    # don't own, and we must never reach across that boundary.
    if job.video_path:
        video_file = Path(job.video_path)
        try:
            uploads_resolved = job_state.UPLOADS_DIR.resolve()
            video_resolved = video_file.resolve()
            sep = "\\" if "\\" in str(uploads_resolved) else "/"
            if str(video_resolved).startswith(str(uploads_resolved) + sep):
                if video_file.exists():
                    video_file.unlink()
                    logger.info(
                        "[Job {}] removed source video {}",
                        job_id[:8], video_file.name,
                    )
        except Exception as exc:  # noqa: BLE001 — best-effort cleanup.
            logger.warning(
                "[Job {}] could not remove source video {}: {}",
                job_id[:8], job.video_path, exc,
            )

    job_state.jobs.pop(job_id, None)
    return {"deleted": job_id}


@router.get("/api/download/{job_id}")
async def download_output(job_id: str):
    job = _get_job_or_404(job_id)
    if job.status != JobStatus.COMPLETED or not job.output_file:
        raise HTTPException(status_code=404, detail="Output not ready")
    path = Path(job.output_file)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Output file missing")
    return FileResponse(
        path=str(path),
        filename=path.name,
        media_type="video/mp4",
    )


# ─── Job Creation ─────────────────────────────────────────────────────────────


@router.post("/api/jobs")
async def create_job(
    background_tasks: BackgroundTasks,
    video: UploadFile = File(...),
    target_language: str = Form("en"),
    transcribe_only: bool = Form(False),
    num_speakers: int | None = Form(None),
    speaker_detection: bool = Form(True),
):
    """Create a Job from an uploaded video and start the pipeline."""
    if not (video.filename or "").lower().endswith(
        (".mp4", ".mov", ".mkv", ".avi")
    ):
        raise HTTPException(
            status_code=400,
            detail="Only video files are accepted (.mp4, .mov, .mkv, .avi)",
        )

    if num_speakers is not None and not (1 <= num_speakers <= 6):
        raise HTTPException(
            status_code=400, detail="num_speakers must be between 1 and 6",
        )

    job_id = uuid.uuid4().hex[:12]
    safe_name = safe_upload_name(video.filename)
    upload_path = job_state.UPLOADS_DIR / f"{job_id}_{safe_name}"

    # Stream upload chunk-by-chunk so multi-GB files don't blow RAM, and
    # enforce config.MAX_UPLOAD_BYTES so a malicious client can't fill disk.
    try:
        await save_upload_streaming(video, upload_path)
    except UploadTooLargeError as exc:
        upload_path.unlink(missing_ok=True)
        raise HTTPException(status_code=413, detail=str(exc))

    job = Job(
        id=job_id,
        filename=video.filename,
        target_language=target_language,
        status=JobStatus.QUEUED,
        created_at=time.time(),
        video_path=str(upload_path),
        transcribe_only=transcribe_only,
        num_speakers=num_speakers,
        speaker_detection=speaker_detection,
    )
    job_state.jobs[job_id] = job

    if transcribe_only:
        task = asyncio.create_task(
            _run_transcription_only(job_id, upload_path, target_language)
        )
    else:
        task = asyncio.create_task(
            _run_render_pipeline(
                job_id=job_id,
                video_path=upload_path,
                target_language=target_language,
                style_config={},
            )
        )
    job_state.track_task(job_state.job_tasks, job_id, task)

    return job.model_dump(exclude={"log_lines"})


# ─── Transcript endpoints ────────────────────────────────────────────────────


@router.get("/api/jobs/{job_id}/transcript")
async def get_transcript(job_id: str) -> dict:
    """Return Phase 1 transcript in a preview-ready format."""
    job = _get_job_or_404(job_id)

    if not job.transcript_path:
        raise HTTPException(
            status_code=404,
            detail="Transcript not available yet. Wait for phase 1 to complete.",
        )

    transcript_file = Path(job.transcript_path)
    if not transcript_file.exists():
        raise HTTPException(
            status_code=404, detail="Transcript file not found on disk.",
        )

    with transcript_file.open("r", encoding="utf-8") as f:
        raw = json.load(f)

    segments: list[dict] = []
    iterable = raw.get("segments", raw if isinstance(raw, list) else [])
    for seg in iterable:
        entry = {
            "start": seg.get("start", 0),
            "end": seg.get("end", 0),
            "text": seg.get("text", "").strip(),
            "speaker": seg.get("speaker", "SPEAKER_00"),
        }
        if "words" in seg and seg["words"]:
            entry["words"] = [
                {
                    "word": w.get("word", w.get("text", "")),
                    "start": w.get("start", 0),
                    "end": w.get("end", 0),
                }
                for w in seg["words"]
            ]
        if entry["text"]:
            segments.append(entry)

    return {"segments": segments, "job_id": job_id}


@router.put("/api/jobs/{job_id}/transcript")
async def update_transcript(job_id: str, req: TranscriptUpdateRequest) -> dict:
    """Save user edits from the preview editor to disk."""
    job = _get_job_or_404(job_id)

    if not req.segments:
        raise HTTPException(
            status_code=400, detail="segments tidak boleh kosong.",
        )

    if job.transcript_path:
        transcript_file = Path(job.transcript_path)
    else:
        transcript_file = (
            job_state.OUTPUT_ROOT
            / job_id
            / "phase1_transcription"
            / "source_transcript.json"
        )
        transcript_file.parent.mkdir(parents=True, exist_ok=True)
        job.transcript_path = str(transcript_file)

    with transcript_file.open("w", encoding="utf-8") as f:
        json.dump({"segments": req.segments}, f, ensure_ascii=False, indent=2)

    return {
        "job_id": job_id,
        "saved": True,
        "segments_count": len(req.segments),
    }


@router.get("/api/jobs/{job_id}/transcript/original")
async def get_original_transcript(job_id: str) -> dict:
    """Return the original ElevenLabs transcript (pre-sanitization, pre-Gemini).

    Preference order:
      1. ``elevenlabs_words_raw.json`` — saved BEFORE sanitize_timestamps
         runs; the closest representation of what the ElevenLabs API
         actually reported (only structural reshaping into segments,
         no timing mutation).
      2. ``elevenlabs_original_transcript.json`` — legacy file, saved
         AFTER the in-processor sanitize ran but BEFORE Gemini
         regrouping/translation. Kept as a fallback for jobs created
         before the raw-file change.
    """
    _get_job_or_404(job_id)

    output_dir = job_state.OUTPUT_ROOT / job_id
    raw_file = output_dir / "phase1_transcription" / "elevenlabs_words_raw.json"
    legacy_file = (
        output_dir / "phase1_transcription"
        / "elevenlabs_original_transcript.json"
    )

    if raw_file.exists():
        original_file = raw_file
    elif legacy_file.exists():
        original_file = legacy_file
    else:
        raise HTTPException(
            status_code=404,
            detail="Original ElevenLabs transcript not available for this job.",
        )

    with original_file.open("r", encoding="utf-8") as f:
        raw = json.load(f)

    # The on-disk format has changed over time:
    #   - new jobs: ``{"segments": [...]}`` dict wrapper
    #   - older jobs: a bare list of segments
    # Normalise to a list of segment dicts up front so the rest of
    # the function only has to deal with one shape.
    if isinstance(raw, list):
        raw_segments = raw
    elif isinstance(raw, dict):
        raw_segments = raw.get("segments", [])
    else:
        raw_segments = []

    segments: list[dict] = []
    for seg in raw_segments:
        if not isinstance(seg, dict):
            continue
        entry = {
            "start": seg.get("start", 0),
            "end": seg.get("end", 0),
            "text": seg.get("text", "").strip(),
            "speaker": seg.get("speaker", "SPEAKER_00"),
        }
        if "words" in seg and seg["words"]:
            entry["words"] = [
                {
                    "word": w.get("word", w.get("text", "")),
                    "start": w.get("start", 0),
                    "end": w.get("end", 0),
                }
                for w in seg["words"]
            ]
        if entry["text"]:
            segments.append(entry)

    return {"segments": segments, "job_id": job_id}


# ─── Video stream ────────────────────────────────────────────────────────────


@router.get("/api/jobs/{job_id}/video")
async def get_video(job_id: str):
    """Stream the uploaded video into the preview player."""
    job = _get_job_or_404(job_id)

    if not job.video_path:
        raise HTTPException(status_code=404, detail="Video path not found.")

    video_file = Path(job.video_path)
    if not video_file.exists():
        raise HTTPException(
            status_code=404, detail="Video file not found on disk.",
        )

    suffix = video_file.suffix.lower()
    media_type_map = {
        ".mp4": "video/mp4",
        ".mov": "video/quicktime",
        ".mkv": "video/x-matroska",
        ".avi": "video/x-msvideo",
    }
    return FileResponse(
        path=str(video_file),
        media_type=media_type_map.get(suffix, "video/mp4"),
        headers={"Accept-Ranges": "bytes"},
    )


# ─── Render trigger + AE export ──────────────────────────────────────────────


@router.post("/api/jobs/{job_id}/render")
async def start_render(job_id: str, req: RenderRequest) -> dict:
    """Resume the pipeline at Phase 2 → Phase 4 with the user's style."""
    job = _get_job_or_404(job_id)

    if job.status == JobStatus.RUNNING:
        raise HTTPException(status_code=409, detail="Job is already running.")

    if job.status == JobStatus.COMPLETED and not job.transcribe_only:
        raise HTTPException(status_code=409, detail="Job already completed.")

    if not job.video_path:
        raise HTTPException(
            status_code=400, detail="No video path stored for this job.",
        )

    job.status = JobStatus.QUEUED
    job.current_phase = 1
    job.progress_pct = 25.0
    job.phase_label = "Starting render..."
    job.output_file = None
    job.error = None
    job.transcribe_only = False

    video_path = Path(job.video_path)

    task = asyncio.create_task(
        _run_render_pipeline(
            job_id=job_id,
            video_path=video_path,
            target_language=job.target_language,
            style_config=req.style_config,
        )
    )
    job_state.track_task(job_state.job_tasks, job_id, task)

    return job.model_dump(exclude={"log_lines"})


@router.post("/api/jobs/{job_id}/export-ae")
async def export_after_effects(job_id: str, req: RenderRequest):
    """Generate an After Effects ExtendScript (.jsx) from transcript + style."""
    from fastapi.responses import Response  # local import — narrow scope

    job = _get_job_or_404(job_id)

    style = dict(req.style_config)
    transcript = style.pop("transcript", [])
    video_duration = float(style.pop("videoDuration", 60.0))
    video_width = int(style.pop("videoWidth", 1920))
    video_height = int(style.pop("videoHeight", 1080))
    fps = float(style.pop("fps", 30.0))

    # If no transcript in request, load from file.
    if not transcript and job.transcript_path:
        transcript_file = Path(job.transcript_path)
        if transcript_file.exists():
            with transcript_file.open("r", encoding="utf-8") as f:
                raw = json.load(f)
            transcript = raw.get("segments", [])

    if not transcript:
        raise HTTPException(status_code=400, detail="No transcript data available.")

    from processors.ae_export import generate_ae_script
    jsx_content = generate_ae_script(
        segments=transcript,
        style_config=style,
        video_width=video_width,
        video_height=video_height,
        video_duration=video_duration,
        fps=fps,
    )

    return Response(
        content=jsx_content,
        media_type="application/javascript",
        headers={
            "Content-Disposition": (
                f'attachment; filename="subtitles_{job_id}.jsx"'
            ),
        },
    )


# ─── Pipeline runner wrappers ────────────────────────────────────────────────


async def _run_transcription_only(
    job_id: str,
    video_path: Path,
    target_language: str,
) -> None:
    """Adapt route layer (job_id) to service layer (Job object)."""
    from web.services.pipeline_runner import (
        run_transcription_only as _run_transcription_only_impl,
    )
    await _run_transcription_only_impl(
        job_state.jobs[job_id], video_path, target_language,
    )


async def _run_render_pipeline(
    job_id: str,
    video_path: Path,
    target_language: str,
    style_config: dict,
) -> None:
    """Adapt route layer (job_id) to service layer (Job object)."""
    from web.services.pipeline_runner import (
        run_render_pipeline as _run_render_pipeline_impl,
    )
    await _run_render_pipeline_impl(
        job_state.jobs[job_id], video_path, target_language, style_config,
    )


# ─── SSE log stream ──────────────────────────────────────────────────────────


@router.get("/api/jobs/{job_id}/log")
async def stream_log(job_id: str):
    """SSE log stream for an auto-subtitle Job."""
    _get_job_or_404(job_id)

    async def event_generator() -> AsyncGenerator[str, None]:
        sent = 0
        while True:
            job = job_state.jobs.get(job_id)
            if not job:
                break
            for line in job.log_lines[sent:]:
                yield f"data: {json.dumps({'line': line})}\n\n"
                sent += 1
            if job.status in (
                JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED,
            ):
                yield (
                    f"data: {json.dumps({'done': True, 'status': job.status})}"
                    "\n\n"
                )
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ─── /api/jobs/from-clip ─────────────────────────────────────────────────────


@router.post("/api/jobs/from-clip")
async def create_job_from_clip(
    background_tasks: BackgroundTasks,
    clip_path: str = Form(...),
    target_language: str = Form("en"),
    num_speakers: int | None = Form(None),
    speaker_detection: bool = Form(True),
):
    """Create an auto-subtitle Job from an existing Clip Finder video file."""
    clip_file = Path(clip_path)
    if not clip_file.exists():
        raise HTTPException(status_code=404, detail="Clip file not found")

    if not clip_file.name.lower().endswith((".mp4", ".mov", ".mkv", ".avi")):
        raise HTTPException(
            status_code=400, detail="Only video files are accepted",
        )

    if num_speakers is not None and not (1 <= num_speakers <= 6):
        raise HTTPException(
            status_code=400, detail="num_speakers must be between 1 and 6",
        )

    job_id = uuid.uuid4().hex[:12]

    job = Job(
        id=job_id,
        filename=clip_file.name,
        target_language=target_language,
        status=JobStatus.QUEUED,
        created_at=time.time(),
        video_path=str(clip_file),
        transcribe_only=True,
        num_speakers=num_speakers,
        speaker_detection=speaker_detection,
    )
    job_state.jobs[job_id] = job

    task = asyncio.create_task(
        _run_transcription_only(job_id, clip_file, target_language)
    )
    job_state.track_task(job_state.job_tasks, job_id, task)

    return job.model_dump(exclude={"log_lines"})


__all__ = [
    "router",
    "register_restore_hook",
    "TranscriptUpdateRequest",
    "RenderRequest",
]
