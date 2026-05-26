"""
web/server.py — FastAPI backend for the Video Clip Automation System web UI.

Flow (2-tahap):
  1. POST /api/jobs               — Upload video, jalankan HANYA Phase 1 (transkripsi)
  2. GET  /api/jobs/{id}/transcript — Ambil hasil transkripsi setelah phase 1 selesai
  3. GET  /api/jobs/{id}/video      — Stream video asli ke preview player
  4. POST /api/jobs/{id}/render     — Lanjutkan pipeline (phase 2-4) dengan style config dari UI

Endpoints lain:
  GET  /api/jobs/{id}     — Get job status + phase progress
  GET  /api/jobs/{id}/log — Stream live log lines (SSE)
  GET  /api/jobs          — List all jobs
  GET  /api/download/{id} — Download final output file
  DELETE /api/jobs/{id}   — Cancel / remove a job
  GET  /api/system        — System info (CUDA, packages, etc.)
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import time
import uuid
from enum import Enum
from pathlib import Path
from typing import AsyncGenerator

# torch is optional — only used for surfacing GPU info on /api/system.
# Removing whisperx makes torch a hard-to-install transitive dependency we
# no longer need at the runtime path, so import it defensively.
try:
    import torch  # type: ignore
    _HAS_TORCH = True
except ImportError:  # pragma: no cover
    torch = None  # type: ignore
    _HAS_TORCH = False

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from loguru import logger
from pydantic import BaseModel

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))
import config

from web.routes.system import router as system_router
from web.routes.pages import build_page_router
from web.routes.short_maker import router as short_maker_router
from web.routes.clip_finder import (
    router as clip_finder_router,
    register_restore_hook as _register_cf_restore,
)
from web.routes.all_in import (
    router as all_in_router,
    register_restore_hook as _register_all_in_restore,
)
from web.services.upload_helpers import (
    UploadTooLargeError as _UploadTooLargeError,
    safe_upload_name as _safe_upload_name,
    save_upload_streaming as _save_upload_streaming,
)
from web.services import job_state as _job_state
# Re-export the shared workspace state under their legacy names so
# every existing call site in this module keeps working unchanged.
# The router-extraction commits introduce typed imports from
# ``web.services.job_state`` directly; the aliases here cover the
# transition window.
_jobs = _job_state.jobs
_job_tasks = _job_state.job_tasks
_cf_jobs = _job_state.cf_jobs
_cf_tasks = _job_state.cf_tasks
_short_jobs = _job_state.short_jobs
_short_tasks = _job_state.short_tasks
_all_in_jobs = _job_state.all_in_jobs
_all_in_tasks = _job_state.all_in_tasks
UPLOADS_DIR = _job_state.UPLOADS_DIR
OUTPUT_ROOT = _job_state.OUTPUT_ROOT
CLIP_FINDER_DIR = _job_state.CLIP_FINDER_DIR
ALL_IN_DIR = _job_state.ALL_IN_DIR

# ─── App Setup ────────────────────────────────────────────────────────────────

app = FastAPI(
    title="CLIP-AUTOMATION API",
    description="Video Clip Automation System — Auto-subtitle Pipeline",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount workspace-scoped routers. Each router lives in web/routes/* and
# owns its own URL prefix; ``web/server.py`` only handles wiring,
# template responses, and Job lifecycle that pre-dates the split.
app.include_router(system_router)
app.include_router(short_maker_router)
app.include_router(clip_finder_router)
_register_cf_restore(app)
app.include_router(all_in_router)
_register_all_in_restore(app)

# Serve static frontend files
STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)

# Jinja2 templates — multi-page editorial structure
TEMPLATES_DIR = Path(__file__).parent / "templates"
TEMPLATES_DIR.mkdir(exist_ok=True)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Page router lives in web/routes/pages.py — wired here because the
# Jinja2Templates instance is built right above and the factory takes
# it as an argument (keeps web/routes/pages.py free of filesystem I/O
# at import time).
app.include_router(build_page_router(templates))


def _is_job_id(name: str) -> bool:
    """Return True if name looks like a 12-char hex job ID."""
    return bool(re.match(r'^[0-9a-f]{12}$', name))


# ─── Upload streaming helpers ────────────────────────────────────────────────
# Implementation lives in web/services/upload_helpers.py — imported above
# as _UploadTooLargeError / _safe_upload_name / _save_upload_streaming so
# every existing call site keeps working without renaming.


@app.on_event("startup")
async def restore_jobs_from_disk() -> None:
    """Scan the output/ directory on startup and restore jobs that have a
    saved transcript so they appear in the Recent Jobs list."""
    if not OUTPUT_ROOT.exists():
        return

    restored = 0
    for job_dir in sorted(OUTPUT_ROOT.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not job_dir.is_dir():
            continue
        job_id = job_dir.name
        if not _is_job_id(job_id):
            continue
        if job_id in _jobs:
            continue  # already tracked in memory

        transcript_file = job_dir / "phase1_transcription" / "source_transcript.json"
        if not transcript_file.exists():
            continue

        meta_file = job_dir / "job_meta.json"
        try:
            if meta_file.exists():
                with meta_file.open("r", encoding="utf-8") as f:
                    meta = json.load(f)
                # Only keep keys that exist in the Job model
                valid_keys = set(Job.model_fields.keys())
                filtered = {k: v for k, v in meta.items() if k in valid_keys}
                job = Job(**filtered)
            else:
                # Reconstruct minimal metadata from filesystem
                video_path = None
                filename = "video.mp4"
                for upload in UPLOADS_DIR.glob(f"{job_id}_*"):
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

            _jobs[job_id] = job
            restored += 1
        except Exception as exc:
            logger.warning("Could not restore job {}: {}", job_id, exc)

    if restored:
        logger.info("Restored {} job(s) from output directory", restored)


# ─── Job State ────────────────────────────────────────────────────────────────
#
# Job / JobStatus / PHASE_LABELS live in ``web.services.job_models`` so the
# pipeline runner can import them without dragging the FastAPI app along.
# We re-export them here at the module level for backward-compatibility
# with any code that imports from ``web.server``.

from web.services.job_models import Job, JobStatus, PHASE_LABELS  # noqa: E402
from web.services.pipeline_runner import (  # noqa: E402
    run_render_pipeline as _run_render_pipeline_impl,
    run_transcription_only as _run_transcription_only_impl,
)
from web.services.transcript_sync import (  # noqa: E402
    sync_segment_words_with_text as _sync_segment_words_with_text,
)


# In-memory job store lives in ``web.services.job_state``. The names
# ``_jobs`` and ``_job_tasks`` are aliased near the top of this module
# so existing call sites keep working unchanged.


def _track_job_task(job_id: str, task: asyncio.Task) -> None:
    """Register a background task and ensure ``_job_tasks`` doesn't leak.

    Without the done-callback the dict grows unbounded as completed Tasks
    pile up after each /api/jobs request.  Tying the cleanup to
    Task.add_done_callback keeps the bookkeeping local to the task itself
    instead of scattered across every endpoint that creates one.
    """
    _job_tasks[job_id] = task

    def _cleanup(_t: asyncio.Task) -> None:
        # Only drop the entry if it's still pointing at the same task —
        # a re-render would have replaced it with a fresh Task and we
        # mustn't clobber the new mapping.
        if _job_tasks.get(job_id) is _t:
            _job_tasks.pop(job_id, None)

    task.add_done_callback(_cleanup)


# ─── System Info ──────────────────────────────────────────────────────────────
# Implemented in web/routes/system.py — mounted via app.include_router above.


# ─── Job CRUD ─────────────────────────────────────────────────────────────────

@app.get("/api/jobs")
async def list_jobs():
    jobs = sorted(_jobs.values(), key=lambda j: j.created_at, reverse=True)
    result = []
    for j in jobs:
        d = j.model_dump(exclude={"log_lines", "video_path"})
        d["has_transcript"] = bool(
            j.transcript_path and Path(j.transcript_path).exists()
        )
        result.append(d)
    return result


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    job = _get_job_or_404(job_id)
    return job.model_dump()


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str):
    job = _get_job_or_404(job_id)
    task = _job_tasks.pop(job_id, None)
    if task and not task.done():
        task.cancel()
        # Wait briefly for the task to acknowledge cancellation so we don't
        # leave a half-written transcript / output file behind.  asyncio.shield
        # ensures CancelledError from the task itself doesn't propagate up to
        # the HTTP handler and turn into a 500.
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            # Task either took too long, was cancelled cleanly, or raised —
            # either way, the cancel signal was delivered and the cleanup
            # itself shouldn't block the delete response.
            pass

    # ── Disk cleanup ────────────────────────────────────────────────────
    # Remove the job's output directory (transcript, intermediates, final
    # render) and the uploaded source video.  This matches the user's
    # expectation that "Remove" on a Recent Jobs card frees the disk
    # bytes the job consumed.
    #
    # Only delete the source file if it lives under UPLOADS_DIR — jobs
    # created via /api/jobs/from-clip point at a clip-finder MP4 we
    # don't own, and we must never reach across that boundary.
    output_dir = OUTPUT_ROOT / job_id
    if output_dir.exists() and output_dir.is_dir():
        try:
            shutil.rmtree(output_dir)
            logger.info("[Job {}] removed output dir {}", job_id[:8], output_dir)
        except Exception as exc:
            logger.warning(
                "[Job {}] could not remove {}: {}", job_id[:8], output_dir, exc,
            )

    if job.video_path:
        video_file = Path(job.video_path)
        try:
            uploads_resolved = UPLOADS_DIR.resolve()
            video_resolved = video_file.resolve()
            # Path.is_relative_to landed in 3.9; we know the runtime is
            # 3.11+ but use the str-prefix form so a missing file (which
            # makes resolve() raise on Windows) never breaks delete.
            if str(video_resolved).startswith(str(uploads_resolved) + ("\\" if "\\" in str(uploads_resolved) else "/")):
                if video_file.exists():
                    video_file.unlink()
                    logger.info(
                        "[Job {}] removed source video {}",
                        job_id[:8], video_file.name,
                    )
        except Exception as exc:
            logger.warning(
                "[Job {}] could not remove source video {}: {}",
                job_id[:8], job.video_path, exc,
            )

    _jobs.pop(job_id, None)
    return {"deleted": job_id}


@app.get("/api/download/{job_id}")
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


# ─── Job Creation & Execution ─────────────────────────────────────────────────

@app.post("/api/jobs")
async def create_job(
    background_tasks: BackgroundTasks,
    video: UploadFile = File(...),
    target_language: str = Form("en"),
    transcribe_only: bool = Form(False),
    num_speakers: int | None = Form(None),
    speaker_detection: bool = Form(True),
):
    # Validate file type
    if not video.filename.lower().endswith((".mp4", ".mov", ".mkv", ".avi")):
        raise HTTPException(status_code=400, detail="Only video files are accepted (.mp4, .mov, .mkv, .avi)")

    # Validate num_speakers if provided
    if num_speakers is not None and not (1 <= num_speakers <= 6):
        raise HTTPException(status_code=400, detail="num_speakers must be between 1 and 6")

    job_id = uuid.uuid4().hex[:12]
    safe_name = _safe_upload_name(video.filename)
    upload_path = UPLOADS_DIR / f"{job_id}_{safe_name}"

    # Stream upload chunk-by-chunk so multi-GB files don't blow RAM, and
    # enforce config.MAX_UPLOAD_BYTES so a malicious client can't fill disk.
    try:
        await _save_upload_streaming(video, upload_path)
    except _UploadTooLargeError as exc:
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
    _jobs[job_id] = job

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
    _track_job_task(job_id, task)

    return job.model_dump(exclude={"log_lines"})


class TranscriptUpdateRequest(BaseModel):
    segments: list[dict]


# ─── Endpoint: Transcript ─────────────────────────────────────────────────────

@app.get("/api/jobs/{job_id}/transcript")
async def get_transcript(job_id: str):
    """Ambil hasil transkripsi (Phase 1) dalam format yang bisa dipakai preview."""
    job = _get_job_or_404(job_id)

    if not job.transcript_path:
        raise HTTPException(status_code=404, detail="Transcript not available yet. Wait for phase 1 to complete.")

    transcript_file = Path(job.transcript_path)
    if not transcript_file.exists():
        raise HTTPException(status_code=404, detail="Transcript file not found on disk.")

    with transcript_file.open("r", encoding="utf-8") as f:
        raw = json.load(f)

    segments = []
    for seg in raw.get("segments", raw if isinstance(raw, list) else []):
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


@app.put("/api/jobs/{job_id}/transcript")
async def update_transcript(job_id: str, req: TranscriptUpdateRequest):
    """Simpan perubahan transcript dari preview editor ke disk."""
    job = _get_job_or_404(job_id)

    if not req.segments:
        raise HTTPException(status_code=400, detail="segments tidak boleh kosong.")

    # Tentukan path file — gunakan yang sudah ada, atau buat default
    if job.transcript_path:
        transcript_file = Path(job.transcript_path)
    else:
        transcript_file = Path("./output") / job_id / "phase1_transcription" / "source_transcript.json"
        transcript_file.parent.mkdir(parents=True, exist_ok=True)
        job.transcript_path = str(transcript_file)

    with transcript_file.open("w", encoding="utf-8") as f:
        json.dump({"segments": req.segments}, f, ensure_ascii=False, indent=2)

    return {"job_id": job_id, "saved": True, "segments_count": len(req.segments)}


@app.get("/api/jobs/{job_id}/transcript/original")
async def get_original_transcript(job_id: str):
    """Return the original ElevenLabs transcript (pre-sanitization, pre-Gemini).

    Preference order:
      1. ``elevenlabs_words_raw.json`` — saved BEFORE sanitize_timestamps runs;
         this is the closest representation of what the ElevenLabs API actually
         reported (only structural reshaping into segments, no timing mutation).
      2. ``elevenlabs_original_transcript.json`` — legacy file, saved AFTER the
         in-processor sanitize ran but BEFORE Gemini regrouping/translation.
         Kept as a fallback for jobs created before the raw-file change.
    """
    job = _get_job_or_404(job_id)

    output_dir = Path("./output") / job_id
    raw_file = output_dir / "phase1_transcription" / "elevenlabs_words_raw.json"
    legacy_file = output_dir / "phase1_transcription" / "elevenlabs_original_transcript.json"

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
    # ``.get`` blows up on the list form, which used to crash this route
    # with a 500.  Normalise to a list of segment dicts up front so the
    # rest of the function only has to deal with one shape.
    if isinstance(raw, list):
        raw_segments = raw
    elif isinstance(raw, dict):
        raw_segments = raw.get("segments", [])
    else:
        raw_segments = []

    segments = []
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


# ─── Endpoint: Video Stream ───────────────────────────────────────────────────

@app.get("/api/jobs/{job_id}/video")
async def get_video(job_id: str):
    """Stream video asli yang diupload ke preview player di browser."""
    job = _get_job_or_404(job_id)

    if not job.video_path:
        raise HTTPException(status_code=404, detail="Video path not found.")

    video_file = Path(job.video_path)
    if not video_file.exists():
        raise HTTPException(status_code=404, detail="Video file not found on disk.")

    suffix = video_file.suffix.lower()
    media_type_map = {
        ".mp4": "video/mp4",
        ".mov": "video/quicktime",
        ".mkv": "video/x-matroska",
        ".avi": "video/x-msvideo",
    }
    media_type = media_type_map.get(suffix, "video/mp4")

    return FileResponse(
        path=str(video_file),
        media_type=media_type,
        headers={"Accept-Ranges": "bytes"},
    )


# ─── Endpoint: Render (lanjutkan pipeline dari Phase 2) ──────────────────────

class RenderRequest(BaseModel):
    style_config: dict = {}


@app.post("/api/jobs/{job_id}/render")
async def start_render(job_id: str, req: RenderRequest):
    """
    Lanjutkan pipeline dari Phase 2 ke Phase 4.
    Dipanggil setelah user selesai mengatur subtitle style di preview screen.
    """
    job = _get_job_or_404(job_id)

    if job.status == JobStatus.RUNNING:
        raise HTTPException(status_code=409, detail="Job is already running.")

    if job.status == JobStatus.COMPLETED and not job.transcribe_only:
        raise HTTPException(status_code=409, detail="Job already completed.")

    if not job.video_path:
        raise HTTPException(status_code=400, detail="No video path stored for this job.")

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
    _track_job_task(job_id, task)

    return job.model_dump(exclude={"log_lines"})


# ─── Endpoint: Export After Effects Script ───────────────────────────────────

@app.post("/api/jobs/{job_id}/export-ae")
async def export_after_effects(job_id: str, req: RenderRequest):
    """Generate an After Effects ExtendScript (.jsx) file from transcript + style."""
    job = _get_job_or_404(job_id)

    style = dict(req.style_config)
    transcript = style.pop("transcript", [])
    video_duration = float(style.pop("videoDuration", 60.0))
    video_width = int(style.pop("videoWidth", 1920))
    video_height = int(style.pop("videoHeight", 1080))
    fps = float(style.pop("fps", 30.0))

    # If no transcript in request, load from file
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
            "Content-Disposition": f'attachment; filename="subtitles_{job_id}.jsx"'
        },
    )


# ─── Run render pipeline ────────────────────────────────────────────────────

async def _run_transcription_only(
    job_id: str,
    video_path: Path,
    target_language: str,
) -> None:
    """Thin wrapper that adapts the route layer (job_id) to the service
    layer (Job object).  Implementation lives in
    ``web.services.pipeline_runner.run_transcription_only``."""
    await _run_transcription_only_impl(_jobs[job_id], video_path, target_language)


async def _run_render_pipeline(
    job_id: str,
    video_path: Path,
    target_language: str,
    style_config: dict,
) -> None:
    """Thin wrapper that adapts the route layer (job_id) to the service
    layer (Job object).  Implementation lives in
    ``web.services.pipeline_runner.run_render_pipeline``."""
    await _run_render_pipeline_impl(
        _jobs[job_id], video_path, target_language, style_config,
    )


# ─── SSE Log Stream ───────────────────────────────────────────────────────────

@app.get("/api/jobs/{job_id}/log")
async def stream_log(job_id: str):
    """Server-Sent Events endpoint for live log streaming."""
    _get_job_or_404(job_id)

    async def event_generator() -> AsyncGenerator[str, None]:
        sent = 0
        while True:
            job = _jobs.get(job_id)
            if not job:
                break
            lines = job.log_lines[sent:]
            for line in lines:
                yield f"data: {json.dumps({'line': line})}\n\n"
                sent += 1
            if job.status in (
                JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED
            ):
                yield f"data: {json.dumps({'done': True, 'status': job.status})}\n\n"
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


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _get_job_or_404(job_id: str) -> Job:
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    return job


# ═══════════════════════════════════════════════════════════════════════════════
# CLIP FINDER — YouTube Clip Detection via yt-dlp + Gemini AI
# ═══════════════════════════════════════════════════════════════════════════════
# Implemented in web/routes/clip_finder.py — mounted via app.include_router
# at the top of this module. The startup restore hook is wired in via
# register_restore_hook().


@app.get("/api/clip-finder/available-clips")
async def list_available_clips():
    """List all clip finder jobs that have downloaded clips, for use in auto-subtitle."""
    result = []
    if not CLIP_FINDER_DIR.exists():
        return result

    for job_dir in sorted(CLIP_FINDER_DIR.iterdir(), reverse=True):
        if not job_dir.is_dir():
            continue
        clips_dir = job_dir / "clips"
        if not clips_dir.exists():
            continue

        clip_files = sorted(clips_dir.glob("*.mp4"))
        if not clip_files:
            continue

        # Check if we have in-memory job info
        job_id = job_dir.name
        cf_job = _cf_jobs.get(job_id)

        clips_list = []
        for i, clip_file in enumerate(clip_files):
            clip_info: dict = {
                "index": i,
                "filename": clip_file.name,
                "path": str(clip_file),
                "size": clip_file.stat().st_size,
            }
            # Attach metadata from job data if available
            if cf_job and i < len(cf_job.clips):
                src = cf_job.clips[i]
                clip_info["title"] = src.get("title", "")
                clip_info["reason"] = src.get("reason", "")
                clip_info["start"] = src.get("start", 0)
                clip_info["end"] = src.get("end", 0)
                if "score" in src:
                    clip_info["score"] = src["score"]
                if "highlight_type" in src:
                    clip_info["highlight_type"] = src["highlight_type"]
                if "hunter" in src:
                    clip_info["hunter"] = src["hunter"]
            else:
                clip_info["title"] = clip_file.stem
            clips_list.append(clip_info)

        result.append({
            "job_id": job_id,
            "url": cf_job.url if cf_job else None,
            "video_title": cf_job.video_title if cf_job else None,
            "clip_count": len(clips_list),
            "clips": clips_list,
        })

    return result


@app.post("/api/jobs/from-clip")
async def create_job_from_clip(
    background_tasks: BackgroundTasks,
    clip_path: str = Form(...),
    target_language: str = Form("en"),
    num_speakers: int | None = Form(None),
    speaker_detection: bool = Form(True),
):
    """Create an auto-subtitle job from an existing clip finder video file."""
    clip_file = Path(clip_path)
    if not clip_file.exists():
        raise HTTPException(status_code=404, detail="Clip file not found")

    if not clip_file.name.lower().endswith((".mp4", ".mov", ".mkv", ".avi")):
        raise HTTPException(status_code=400, detail="Only video files are accepted")

    # Validate num_speakers if provided
    if num_speakers is not None and not (1 <= num_speakers <= 6):
        raise HTTPException(status_code=400, detail="num_speakers must be between 1 and 6")

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
    _jobs[job_id] = job

    task = asyncio.create_task(
        _run_transcription_only(job_id, clip_file, target_language)
    )
    _track_job_task(job_id, task)

    return job.model_dump(exclude={"log_lines"})


@app.post("/api/clip-finder/jobs")
async def _moved_create_clip_finder_job():
    """Implementation moved to web/routes/clip_finder.py."""
    raise HTTPException(status_code=500, detail="Route extraction in flight")


# ─── Double-Check Helpers ────────────────────────────────────────────────────


def _find_autosub_for_clip(clip_path: Path) -> Path | None:
    """Find the _autosub.json file adjacent to a clip MP4."""
    # Pattern: clip_001_Title_autosub.json next to clip_001_Title.mp4
    autosub_candidate = Path(str(clip_path.with_suffix("")) + "_autosub.json")
    if autosub_candidate.exists():
        return autosub_candidate

    # Fallback: search in same directory for any autosub matching clip index
    stem = clip_path.stem
    prefix_match = re.match(r"(clip_\d+)", stem)
    if prefix_match:
        prefix = prefix_match.group(1)
        for candidate in clip_path.parent.glob(f"{prefix}*_autosub.json"):
            return candidate

    return None


# ─── Short Maker ─────────────────────────────────────────────────────────────
# Implemented in web/routes/short_maker.py — mounted via app.include_router
# at the top of this module. Eight endpoints + one background task all
# share state through web/services/job_state.py.


# ═══════════════════════════════════════════════════════════════════════════════
# ALL IN — Workspace 04 (one-shot orchestrator)
# ═══════════════════════════════════════════════════════════════════════════════
# Implemented in web/routes/all_in.py — mounted via app.include_router at the
# top of this module. The startup restore hook is wired in via
# register_restore_hook() so this module no longer owns AllIn lifecycle.


@app.get("/api/all-in/jobs")
async def list_all_in_jobs():
    jobs = sorted(_all_in_jobs.values(), key=lambda j: j.created_at, reverse=True)
    return [j.model_dump(exclude={"log_lines", "transcript"}) for j in jobs]


@app.delete("/api/all-in/jobs/{job_id}")
async def delete_all_in_job(job_id: str):
    """Delete an All In Job and its on-disk source + clips (Q12)."""
    job = _all_in_jobs.pop(job_id, None)
    if not job:
        raise HTTPException(status_code=404, detail="All In job not found")
    task = _all_in_tasks.pop(job_id, None)
    if task and not task.done():
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            pass

    job_dir = ALL_IN_DIR / job_id
    if job_dir.exists():
        try:
            shutil.rmtree(job_dir)
        except Exception as exc:
            logger.warning("[AllIn {}] rmtree failed: {}", job_id[:8], exc)

    return {"deleted": job_id}


# ─── Page Routes (Jinja2 templates) ──────────────────────────────────────────
# Implemented in web/routes/pages.py — mounted via build_page_router(templates)
# right after the Jinja2Templates instance is built (see top of file).


# ─── Mount Static Files (must be last) ───────────────────────────────────────

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
