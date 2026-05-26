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

# Serve static frontend files
STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)

# Jinja2 templates — multi-page editorial structure
TEMPLATES_DIR = Path(__file__).parent / "templates"
TEMPLATES_DIR.mkdir(exist_ok=True)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

UPLOADS_DIR = Path("./output/uploads")
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

OUTPUT_ROOT = Path("./output")
OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)


def _is_job_id(name: str) -> bool:
    """Return True if name looks like a 12-char hex job ID."""
    return bool(re.match(r'^[0-9a-f]{12}$', name))


# ─── Upload streaming helpers ────────────────────────────────────────────────
#
# UploadFile.read() materialises the entire upload in RAM, which OOMs on
# multi-GB videos and lets a single client exhaust the server's memory.
# These helpers stream the body to disk in fixed-size chunks and enforce a
# configurable size cap so abuse is bounded.

class _UploadTooLargeError(Exception):
    """Raised when the streaming upload exceeds config.MAX_UPLOAD_BYTES."""


def _safe_upload_name(filename: str | None) -> str:
    """Sanitise an uploaded filename for safe filesystem use.

    Strips path separators, collapses whitespace, removes anything outside
    a conservative whitelist, and caps total length so we never overflow
    Windows' MAX_PATH when prepending the job_id prefix.
    """
    name = (filename or "video.mp4").strip()
    # Drop any directory components a malicious client may have included.
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    # Whitelist: alnum, dot, dash, underscore. Replace everything else.
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    # Avoid double-extensions and absurd lengths.
    if len(name) > 120:
        stem, _, ext = name.rpartition(".")
        name = (stem[: 120 - (len(ext) + 1)] + "." + ext) if ext else name[:120]
    return name or "video.mp4"


async def _save_upload_streaming(
    upload: UploadFile,
    dest: Path,
    *,
    chunk_bytes: int | None = None,
    max_bytes: int | None = None,
) -> int:
    """Stream ``upload`` to ``dest`` and return the number of bytes written.

    Raises :class:`_UploadTooLargeError` once the running total would
    exceed ``max_bytes`` (defaults to ``config.MAX_UPLOAD_BYTES``).  The
    partially-written file is left in place; the caller is responsible
    for unlinking it after handling the error.
    """
    chunk_size: int = chunk_bytes or getattr(config, "UPLOAD_CHUNK_BYTES", 1024 * 1024)
    max_size: int = max_bytes or getattr(config, "MAX_UPLOAD_BYTES", 4 * 1024 * 1024 * 1024)

    written = 0
    try:
        with dest.open("wb") as out:
            while True:
                chunk = await upload.read(chunk_size)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_size:
                    raise _UploadTooLargeError(
                        f"Upload exceeds limit of {max_size} bytes "
                        f"(received at least {written}). "
                        "Increase MAX_UPLOAD_BYTES in .env or upload a smaller file."
                    )
                out.write(chunk)
    finally:
        await upload.close()
    return written


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


# In-memory job store (replace with Redis/DB for production)
_jobs: dict[str, Job] = {}
_job_tasks: dict[str, asyncio.Task] = {}


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

@app.get("/api/system")
async def get_system_info():
    if _HAS_TORCH and torch is not None:
        cuda_available = torch.cuda.is_available()
        gpu_name = torch.cuda.get_device_name(0) if cuda_available else None
        torch_version = torch.__version__
    else:
        cuda_available = False
        gpu_name = None
        torch_version = None
    return {
        "cuda_available": cuda_available,
        "gpu_name": gpu_name,
        "torch_version": torch_version,
        "python_version": sys.version.split()[0],
        "packages": {
            "elevenlabs": bool(config.ELEVENLABS_API_KEYS),
            "pycaps": _check_package("pycaps"),
            "ffmpeg": _check_ffmpeg(),
        },
        "env": {
            "elevenlabs_key_set": bool(config.ELEVENLABS_API_KEY),
            "gemini_keys_set": bool(config.GEMINI_API_KEYS),
            "deepl_key_set": bool(getattr(config, "DEEPL_API_KEY", "")),
        },
        # Always ElevenLabs — kept as a single-entry dict so existing UI
        # code that expects a model dropdown still works (it will simply
        # render one option).
        "stt_engines": {
            "elevenlabs": {
                "label": "ElevenLabs Speech-to-Text",
                "description": "Cloud-based STT — auto-translate via Gemini to target language",
                "type": "elevenlabs",
            },
        },
    }


def _check_package(name: str) -> bool:
    try:
        __import__(name)
        return True
    except ImportError:
        return False


def _check_ffmpeg() -> bool:
    if shutil.which("ffmpeg") is not None:
        return True
    try:
        ffmpeg_path = getattr(config, "FFMPEG_PATH", None)
        if ffmpeg_path and Path(ffmpeg_path).is_file():
            return True
    except Exception:
        pass
    return False


# ─── ElevenLabs Quota ────────────────────────────────────────────────────────

@app.get("/api/elevenlabs/quota")
async def get_elevenlabs_quota():
    """Fetch ElevenLabs subscription usage for every configured API key."""
    if not config.ELEVENLABS_API_KEYS:
        raise HTTPException(status_code=400, detail="No ELEVENLABS_API_KEY configured")

    import asyncio
    import httpx

    async def _fetch_one(api_key: str, key_idx: int) -> dict:
        key_label = f"Key #{key_idx + 1}"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    "https://api.elevenlabs.io/v1/user/subscription",
                    headers={"xi-api-key": api_key},
                )
            if resp.status_code != 200:
                return {"key_label": key_label, "error": f"HTTP {resp.status_code}"}
            data = resp.json()
            return {
                "key_label": key_label,
                "character_count": data.get("character_count", 0),
                "character_limit": data.get("character_limit", 0),
                "tier": data.get("tier", "unknown"),
                "next_reset_unix": data.get("next_character_count_reset_unix", 0),
            }
        except httpx.RequestError as exc:
            return {"key_label": key_label, "error": str(exc)}

    results = await asyncio.gather(*[
        _fetch_one(key, idx)
        for idx, key in enumerate(config.ELEVENLABS_API_KEYS)
    ])
    return {"keys": list(results)}


# ─── Gemini Quota ────────────────────────────────────────────────────────────

@app.get("/api/gemini/quota")
async def get_gemini_quota():
    """Check Gemini API key validity for every configured key."""
    if not config.GEMINI_API_KEYS:
        raise HTTPException(status_code=400, detail="No GEMINI_API_KEY configured")

    import asyncio
    import httpx

    async def _check_one(api_key: str, key_idx: int) -> dict:
        key_label = f"Key #{key_idx + 1}"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    "https://generativelanguage.googleapis.com/v1beta/models",
                    params={"key": api_key, "pageSize": 1},
                )
            if resp.status_code == 200:
                return {"key_label": key_label, "status": "active"}
            elif resp.status_code == 429:
                return {"key_label": key_label, "status": "rate_limited"}
            elif resp.status_code in (400, 403):
                return {"key_label": key_label, "status": "invalid"}
            else:
                return {"key_label": key_label, "status": "error", "error": f"HTTP {resp.status_code}"}
        except httpx.RequestError as exc:
            return {"key_label": key_label, "status": "error", "error": str(exc)}

    results = await asyncio.gather(*[
        _check_one(key, idx)
        for idx, key in enumerate(config.GEMINI_API_KEYS)
    ])
    return {"keys": list(results)}


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

CLIP_FINDER_DIR = Path("./output/clip_finder")
CLIP_FINDER_DIR.mkdir(parents=True, exist_ok=True)


class ClipFinderJob(BaseModel):
    id: str
    url: str
    instructions: str
    lang: str = "en"
    start_offset: float = 0.0     # Skip first N seconds (for livestreams)
    mode: str = "single-shot"     # "single-shot" | "multi-stage"
    enable_audio_signals: bool = True
    enable_chat_signals: bool = True
    status: str = "queued"          # queued|transcribing|signals|analyzing|analyzed|downloading|completed|failed
    progress_pct: float = 0.0
    phase_label: str = "Queued"
    error: str | None = None
    created_at: float = 0.0
    video_title: str | None = None
    clips: list[dict] = []          # serialized Clip.to_dict()
    clip_files: list[str] = []      # file paths of cut clips
    signals_summary: dict = {}      # { audio_peak: N, chat_spike: M, ... } for UI badges
    log_lines: list[str] = []
    transcript: list[dict] = []     # Full YouTube auto-sub transcript (preserved for double-check)

    class Config:
        use_enum_values = True


_cf_jobs: dict[str, ClipFinderJob] = {}
_cf_tasks: dict[str, asyncio.Task] = {}


class ClipFinderRequest(BaseModel):
    url: str
    instructions: str
    lang: str = "en"
    start_offset: float = 0.0      # Skip first N seconds (for livestreams)
    mode: str | None = None         # override config default
    enable_audio_signals: bool | None = None
    enable_chat_signals: bool | None = None


def _build_clip_finder():
    """Construct a ClipFinder instance using current config values."""
    from processors.clip_finder import ClipFinder
    return ClipFinder(
        cookies_file=getattr(config, "YTDLP_COOKIES_FILE", ""),
        cookies_browser=getattr(config, "YTDLP_COOKIES_BROWSER", ""),
        gemini_model=getattr(config, "CLIP_FINDER_GEMINI_MODEL", "gemini-3.5-flash"),
        cache_dir=getattr(config, "CLIP_FINDER_CACHE_DIR", None),
        ffmpeg_path=getattr(config, "FFMPEG_PATH", "ffmpeg"),
    )


def _persist_cf_job(job: ClipFinderJob) -> None:
    """Save clip-finder job metadata so it survives server restarts."""
    job_dir = CLIP_FINDER_DIR / job.id
    job_dir.mkdir(parents=True, exist_ok=True)
    meta_file = job_dir / "job_meta.json"
    try:
        with meta_file.open("w", encoding="utf-8") as f:
            json.dump(
                job.model_dump(exclude={"log_lines"}),
                f, ensure_ascii=False, indent=2,
            )
    except Exception as exc:
        logger.warning("[ClipFinder {}] could not persist meta: {}", job.id[:8], exc)


@app.on_event("startup")
async def restore_clip_finder_jobs() -> None:
    """Re-hydrate clip-finder jobs from disk on server start."""
    if not CLIP_FINDER_DIR.exists():
        return

    restored = 0
    for job_dir in sorted(CLIP_FINDER_DIR.iterdir(), reverse=True):
        if not job_dir.is_dir():
            continue
        meta_file = job_dir / "job_meta.json"
        if not meta_file.exists():
            continue
        if job_dir.name in _cf_jobs:
            continue
        try:
            with meta_file.open("r", encoding="utf-8") as f:
                meta = json.load(f)
            valid_keys = set(ClipFinderJob.model_fields.keys())
            filtered = {k: v for k, v in meta.items() if k in valid_keys}
            cf_job = ClipFinderJob(**filtered)
            # If clips were downloaded but server died mid-flight, downgrade
            # to the safe "analyzed" state so the UI shows resumed download
            # button instead of an in-progress spinner forever.
            if cf_job.status in ("downloading", "transcribing", "analyzing", "signals"):
                cf_job.status = "analyzed" if cf_job.clips else "failed"
                cf_job.phase_label = (
                    f"Found {len(cf_job.clips)} clip(s) — Resume from disk"
                    if cf_job.clips else "Server restarted before completion"
                )
            _cf_jobs[cf_job.id] = cf_job
            restored += 1
        except Exception as exc:
            logger.warning("[ClipFinder] could not restore {}: {}", job_dir.name, exc)
    if restored:
        logger.info("[ClipFinder] restored {} job(s) from disk", restored)


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
async def create_clip_finder_job(req: ClipFinderRequest):
    """Create a new clip finder job (Phase 1: transcript + AI analysis only)."""
    if not req.url.strip():
        raise HTTPException(status_code=400, detail="URL is required")
    # Instructions are optional — empty means "find all interesting moments"

    gemini_keys = config.GEMINI_API_KEYS
    if not gemini_keys:
        raise HTTPException(status_code=400, detail="No GEMINI_API_KEY set in .env")

    mode = req.mode or getattr(config, "CLIP_FINDER_MODE", "single-shot")
    if mode not in ("single-shot", "multi-stage"):
        raise HTTPException(status_code=400, detail=f"Invalid mode '{mode}'")

    enable_audio = (
        req.enable_audio_signals
        if req.enable_audio_signals is not None
        else getattr(config, "CLIP_FINDER_ENABLE_AUDIO_SIGNALS", True)
    )
    enable_chat = (
        req.enable_chat_signals
        if req.enable_chat_signals is not None
        else getattr(config, "CLIP_FINDER_ENABLE_CHAT_SIGNALS", True)
    )

    job_id = uuid.uuid4().hex[:12]
    job = ClipFinderJob(
        id=job_id,
        url=req.url.strip(),
        instructions=req.instructions.strip() if req.instructions.strip() else "",
        lang=req.lang,
        start_offset=max(0.0, req.start_offset),
        mode=mode,
        enable_audio_signals=bool(enable_audio),
        enable_chat_signals=bool(enable_chat),
        created_at=time.time(),
    )
    _cf_jobs[job_id] = job
    _persist_cf_job(job)

    task = asyncio.create_task(_run_clip_finder_phase1(job_id, gemini_keys))
    _cf_tasks[job_id] = task

    return job.model_dump(exclude={"log_lines", "transcript"})


@app.get("/api/clip-finder/jobs/{job_id}")
async def get_clip_finder_job(job_id: str):
    job = _cf_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Clip finder job not found")
    return job.model_dump(exclude={"log_lines", "transcript"})


@app.get("/api/clip-finder/jobs/{job_id}/log")
async def stream_clip_finder_log(job_id: str):
    """SSE log stream for clip finder job."""
    job = _cf_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Clip finder job not found")

    async def event_generator() -> AsyncGenerator[str, None]:
        sent = 0
        while True:
            j = _cf_jobs.get(job_id)
            if not j:
                break
            lines = j.log_lines[sent:]
            for line in lines:
                yield f"data: {json.dumps({'line': line})}\n\n"
                sent += 1
            if j.status in ("analyzed", "completed", "failed"):
                yield f"data: {json.dumps({'done': True, 'status': j.status})}\n\n"
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/clip-finder/clips/{job_id}/{clip_idx}")
async def download_clip(job_id: str, clip_idx: int):
    """Download a specific clip."""
    clip_path = _resolve_clip_path(job_id, clip_idx)
    if not clip_path.exists():
        raise HTTPException(status_code=404, detail="Clip file missing")

    return FileResponse(
        path=str(clip_path),
        filename=clip_path.name,
        media_type="video/mp4",
    )


@app.get("/api/clip-finder/clips/{job_id}/{clip_idx}/stream")
async def stream_clip(job_id: str, clip_idx: int):
    """Stream a clip for preview playback."""
    clip_path = _resolve_clip_path(job_id, clip_idx)
    if not clip_path.exists():
        raise HTTPException(status_code=404, detail="Clip file missing")

    return FileResponse(
        path=str(clip_path),
        media_type="video/mp4",
        headers={"Accept-Ranges": "bytes"},
    )


def _resolve_clip_path(job_id: str, clip_idx: int) -> Path:
    """Resolve clip file by index from in-memory job or fallback filesystem directory."""
    if clip_idx < 0:
        raise HTTPException(status_code=404, detail="Clip not found")

    job = _cf_jobs.get(job_id)
    if job and clip_idx < len(job.clip_files) and job.clip_files[clip_idx]:
        return Path(job.clip_files[clip_idx])

    clips_dir = CLIP_FINDER_DIR / job_id / "clips"
    if not clips_dir.exists():
        raise HTTPException(status_code=404, detail="Job not found")

    clip_files = sorted(clips_dir.glob("*.mp4"))
    if clip_idx >= len(clip_files):
        raise HTTPException(status_code=404, detail="Clip not found")

    return clip_files[clip_idx]


@app.post("/api/clip-finder/jobs/{job_id}/download-clips")
async def start_clip_download(job_id: str):
    """Phase 2: Download only the relevant clip sections from YouTube."""
    job = _cf_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Clip finder job not found")
    if job.status not in ("analyzed", "completed"):
        raise HTTPException(status_code=400, detail=f"Job is not ready for download (status: {job.status})")
    if not job.clips:
        raise HTTPException(status_code=400, detail="No clips to download")

    # Reset for Phase 2
    job.status = "downloading"
    job.phase_label = f"Downloading {len(job.clips)} clips..."
    job.progress_pct = 70.0
    job.clip_files = []

    task = asyncio.create_task(_run_clip_download(job_id))
    _cf_tasks[job_id] = task

    return job.model_dump(exclude={"log_lines", "transcript"})


@app.post("/api/clip-finder/jobs/{job_id}/download-clip/{clip_idx}")
async def start_single_clip_download(job_id: str, clip_idx: int):
    """Download a single clip by index."""
    job = _cf_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Clip finder job not found")
    if job.status not in ("analyzed", "completed"):
        raise HTTPException(
            status_code=400,
            detail=f"Job is not ready for download (status: {job.status})",
        )
    if clip_idx < 0 or clip_idx >= len(job.clips):
        raise HTTPException(status_code=404, detail="Clip index out of range")

    # Ensure clip_files list is properly sized
    while len(job.clip_files) < len(job.clips):
        job.clip_files.append("")

    # Already downloaded?
    if job.clip_files[clip_idx]:
        return {"status": "already_downloaded", "clip_idx": clip_idx}

    # Start async download for this single clip
    asyncio.create_task(_run_single_clip_download(job_id, clip_idx))
    return {"status": "downloading", "clip_idx": clip_idx}


async def _run_clip_finder_phase1(job_id: str, gemini_keys: list[str]) -> None:
    """Phase 1: transcript + multimodal signals + AI analysis."""
    job = _cf_jobs[job_id]
    job_dir = CLIP_FINDER_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    def log(msg: str):
        job.log_lines.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        logger.info("[ClipFinder {}] {}", job_id[:8], msg)

    try:
        from processors.clip_finder import ClipFinderError  # noqa: F401

        cf = _build_clip_finder()

        # ── Step 1: Extract transcript via yt-dlp ──
        job.status = "transcribing"
        job.phase_label = "Extracting transcript..."
        job.progress_pct = 10.0
        log(f"Step 1/4: Extracting transcript (mode={job.mode})...")

        transcript = await cf.extract_subtitles(
            url=job.url,
            output_dir=job_dir / "subs",
            lang=job.lang,
            log_fn=log,
        )

        if not transcript:
            log("No subtitles found after trying all strategies.")
            job.status = "failed"
            job.phase_label = "Failed — No subtitles available"
            job.error = (
                "No subtitles found for this video. "
                "Tried auto-generated and manual subtitles in multiple languages. "
                "The video may not have any captions available."
            )
            _persist_cf_job(job)
            return

        log(f"Transcript extracted: {len(transcript)} segments")

        # ── Apply start offset (livestream waiting time) ──
        if job.start_offset > 0:
            original_count = len(transcript)
            transcript = cf.filter_transcript_by_offset(transcript, job.start_offset)
            log(
                f"Applied start offset: {job.start_offset}s — "
                f"filtered {original_count} → {len(transcript)} segments"
            )
            if not transcript:
                log("No transcript segments remain after applying start offset.")
                job.status = "failed"
                job.phase_label = "Failed — No content after start offset"
                job.error = (
                    f"No transcript segments found after the {job.start_offset}s "
                    "start offset. Try a smaller offset value."
                )
                _persist_cf_job(job)
                return

        # ── Step 2: Multimodal signals (audio + chat) ──
        job.status = "signals"
        job.phase_label = "Extracting multimodal signals..."
        job.progress_pct = 30.0
        log("Step 2/4: Extracting multimodal signals (audio + chat)...")

        signals = await cf.extract_signals(
            url=job.url,
            output_dir=job_dir / "signals",
            log_fn=log,
            enable_audio=job.enable_audio_signals,
            enable_chat=job.enable_chat_signals,
        )

        # Drop signals that fall before start_offset (after the trim above)
        if job.start_offset > 0 and signals:
            signals = [s for s in signals if s.end > job.start_offset]

        # Build summary for UI badges
        from collections import Counter
        kinds = Counter(s.kind.value for s in signals)
        job.signals_summary = dict(kinds)

        # ── Step 3: AI clip detection ──
        job.status = "analyzing"
        job.phase_label = (
            f"AI analyzing ({job.mode})..."
        )
        job.progress_pct = 55.0
        log(
            f"Step 3/4: Analyzing with Gemini AI "
            f"(mode={job.mode}, {len(gemini_keys)} key(s))..."
        )

        max_count = getattr(config, "CLIP_FINDER_MAX_CLIPS", 12)
        scored_clips = await cf.find_clips(
            transcript=transcript,
            instructions=job.instructions,
            api_keys=gemini_keys,
            mode=job.mode,
            signals=signals,
            log_fn=log,
            max_count=max_count if job.mode == "multi-stage" else None,
        )

        if not scored_clips:
            log("No clips matched your instructions.")
            job.status = "analyzed"
            job.phase_label = "Analysis complete — No matching clips found"
            job.progress_pct = 100.0
            job.clips = []
            _persist_cf_job(job)
            return

        # Step 4: serialize for API/UI
        job.clips = [c.to_dict() for c in scored_clips]
        job.transcript = list(transcript)

        job.status = "analyzed"
        job.phase_label = f"Found {len(scored_clips)} clip(s) — Ready to download"
        job.progress_pct = 100.0
        log(
            f"Analysis complete! Found {len(scored_clips)} clip(s). "
            f"Top score: {max((c.score.total for c in scored_clips), default=0):.2f}/10"
        )
        log("Click 'Download Clips' to fetch the video sections.")
        _persist_cf_job(job)

    except Exception as exc:
        job.status = "failed"
        job.phase_label = "Failed"
        job.error = str(exc)
        log(f"Error: {exc}")
        logger.exception("[ClipFinder {}] Phase 1 failed", job_id[:8])
        _persist_cf_job(job)


async def _run_clip_download(job_id: str) -> None:
    """Phase 2: Download only the relevant clip sections using yt-dlp --download-sections."""
    job = _cf_jobs[job_id]
    job_dir = CLIP_FINDER_DIR / job_id

    def log(msg: str):
        job.log_lines.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        logger.info("[ClipFinder {}] {}", job_id[:8], msg)

    try:
        cf = _build_clip_finder()

        log(f"Downloading {len(job.clips)} clip sections from YouTube...")

        clip_paths = await cf.download_clip_sections(
            url=job.url,
            clips=job.clips,
            output_dir=job_dir / "clips",
            log_fn=log,
        )

        job.clip_files = [str(p) for p in clip_paths]

        # Update clips with file info
        for i, clip in enumerate(job.clips):
            if i < len(clip_paths):
                clip["file_idx"] = i
                clip["filename"] = clip_paths[i].name

        # ── Slice and save auto-subs per clip (for double-check) ──
        if job.transcript:
            log("Slicing auto-subs per clip for double-check...")
            for i, clip in enumerate(job.clips):
                if i < len(clip_paths):
                    sliced = cf.slice_transcript_for_clip(
                        transcript=job.transcript,
                        clip_start=clip["start"],
                        clip_end=clip["end"],
                    )
                    autosub_file = Path(
                        str(clip_paths[i].with_suffix("")) + "_autosub.json"
                    )
                    with autosub_file.open("w", encoding="utf-8") as f:
                        json.dump(sliced, f, ensure_ascii=False, indent=2)
                    log(f"  Clip {i+1}: saved {len(sliced)} auto-sub segments")
        else:
            log("No transcript stored — auto-sub slices not saved")

        # ── Done ──
        job.status = "completed"
        job.phase_label = f"Completed — {len(clip_paths)} clips ready"
        job.progress_pct = 100.0
        log(f"Done! {len(clip_paths)} clips are ready for download.")
        _persist_cf_job(job)

    except Exception as exc:
        job.status = "failed"
        job.phase_label = "Download failed"
        job.error = str(exc)
        log(f"Error: {exc}")
        logger.exception("[ClipFinder {}] Download failed", job_id[:8])
        _persist_cf_job(job)


async def _run_single_clip_download(job_id: str, clip_idx: int) -> None:
    """Download a single clip section by index."""
    job = _cf_jobs[job_id]
    job_dir = CLIP_FINDER_DIR / job_id

    def log(msg: str):
        job.log_lines.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        logger.info("[ClipFinder {}] {}", job_id[:8], msg)

    try:
        cf = _build_clip_finder()
        clip = job.clips[clip_idx]

        log(f"Downloading clip {clip_idx + 1}/{len(job.clips)}: \"{clip.get('title', '')}\"...")

        clip_paths = await cf.download_clip_sections(
            url=job.url,
            clips=[clip],
            output_dir=job_dir / "clips",
            log_fn=log,
            index_offset=clip_idx,
        )

        # Ensure clip_files list is properly sized
        while len(job.clip_files) < len(job.clips):
            job.clip_files.append("")

        if clip_paths:
            job.clip_files[clip_idx] = str(clip_paths[0])
            clip["file_idx"] = clip_idx
            clip["filename"] = clip_paths[0].name

            # Slice and save auto-sub for this clip (for double-check)
            if job.transcript:
                sliced = cf.slice_transcript_for_clip(
                    transcript=job.transcript,
                    clip_start=clip["start"],
                    clip_end=clip["end"],
                )
                autosub_file = Path(
                    str(clip_paths[0].with_suffix("")) + "_autosub.json"
                )
                with autosub_file.open("w", encoding="utf-8") as f:
                    json.dump(sliced, f, ensure_ascii=False, indent=2)

            log(f"Clip {clip_idx + 1} downloaded successfully.")

            # Check if all clips are now downloaded
            if all(f for f in job.clip_files):
                job.status = "completed"
                job.phase_label = f"Completed — {len(job.clip_files)} clips ready"
                job.progress_pct = 100.0
        else:
            log(f"Failed to download clip {clip_idx + 1}.")

        _persist_cf_job(job)

    except Exception as exc:
        log(f"Error downloading clip {clip_idx + 1}: {exc}")
        logger.exception("[ClipFinder {}] Single clip download failed", job_id[:8])
        _persist_cf_job(job)


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

# In-memory store for short maker jobs
_short_jobs: dict[str, dict] = {}
_short_tasks: dict[str, asyncio.Task] = {}

SHORTS_OUTPUT_DIR = Path("./output/shorts")
SHORTS_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


class ShortMakerRequest(BaseModel):
    top_crop: dict | None = None      # {x, y, w, h}
    bottom_crop: dict | None = None   # {x, y, w, h}
    padding: int = 0


@app.post("/api/short-maker/upload")
async def short_maker_upload(video: UploadFile = File(...)):
    """Upload a video for Short Maker processing."""
    if not video.filename.lower().endswith((".mp4", ".mov", ".mkv", ".avi")):
        raise HTTPException(status_code=400, detail="Only video files are accepted (.mp4, .mov, .mkv, .avi)")

    job_id = uuid.uuid4().hex[:12]
    upload_path = UPLOADS_DIR / f"short_{job_id}_{video.filename}"

    with upload_path.open("wb") as f:
        content = await video.read()
        f.write(content)

    _short_jobs[job_id] = {
        "id": job_id,
        "filename": video.filename,
        "video_path": str(upload_path),
        "status": "uploaded",
        "progress": 0,
        "output_file": None,
        "error": None,
        "created_at": time.time(),
        "log_lines": [],
    }

    return {"job_id": job_id, "filename": video.filename}


@app.get("/api/short-maker/{job_id}/video-info")
async def short_maker_video_info(job_id: str):
    """Get video dimensions and default crop regions for the Short Maker UI."""
    sjob = _short_jobs.get(job_id)
    if not sjob:
        raise HTTPException(status_code=404, detail="Short maker job not found")

    from processors.short_maker import (
        get_video_info,
        compute_default_top_crop,
        compute_default_bottom_crop,
    )

    info = await get_video_info(sjob["video_path"])
    top_crop = compute_default_top_crop(info.width, info.height)
    bottom_crop = compute_default_bottom_crop(info.width, info.height)

    return {
        "job_id": job_id,
        "width": info.width,
        "height": info.height,
        "duration": info.duration,
        "fps": info.fps,
        "default_top_crop": top_crop.to_dict(),
        "default_bottom_crop": bottom_crop.to_dict(),
    }


@app.get("/api/short-maker/{job_id}/preview-frame")
async def short_maker_preview_frame(job_id: str, t: float = 0.0):
    """Extract a single preview frame from the uploaded video at timestamp t."""
    sjob = _short_jobs.get(job_id)
    if not sjob:
        raise HTTPException(status_code=404, detail="Short maker job not found")

    from processors.short_maker import extract_preview_frame

    frame_dir = SHORTS_OUTPUT_DIR / job_id / "frames"
    frame_dir.mkdir(parents=True, exist_ok=True)
    frame_path = frame_dir / f"frame_{t:.2f}.jpg"

    if not frame_path.exists():
        await extract_preview_frame(sjob["video_path"], frame_path, timestamp=t)

    return FileResponse(str(frame_path), media_type="image/jpeg")


@app.get("/api/short-maker/{job_id}/video")
async def short_maker_video(job_id: str):
    """Stream the uploaded video for preview in the Short Maker UI."""
    sjob = _short_jobs.get(job_id)
    if not sjob:
        raise HTTPException(status_code=404, detail="Short maker job not found")

    video_file = Path(sjob["video_path"])
    if not video_file.exists():
        raise HTTPException(status_code=404, detail="Video file not found on disk")

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


@app.post("/api/short-maker/{job_id}/process")
async def short_maker_process(job_id: str, req: ShortMakerRequest):
    """Start processing the uploaded video into a YouTube Short."""
    sjob = _short_jobs.get(job_id)
    if not sjob:
        raise HTTPException(status_code=404, detail="Short maker job not found")

    if sjob["status"] == "processing":
        raise HTTPException(status_code=409, detail="Already processing")

    sjob["status"] = "processing"
    sjob["progress"] = 0
    sjob["error"] = None
    sjob["output_file"] = None
    sjob["log_lines"] = []

    task = asyncio.create_task(
        _run_short_maker(job_id, req.top_crop, req.bottom_crop, req.padding)
    )
    _short_tasks[job_id] = task

    return {"job_id": job_id, "status": "processing"}


@app.get("/api/short-maker/{job_id}/status")
async def short_maker_status(job_id: str):
    """Get the status of a short maker job."""
    sjob = _short_jobs.get(job_id)
    if not sjob:
        raise HTTPException(status_code=404, detail="Short maker job not found")
    return {k: v for k, v in sjob.items() if k != "log_lines"}


@app.get("/api/short-maker/{job_id}/download")
async def short_maker_download(job_id: str):
    """Download the generated short video."""
    sjob = _short_jobs.get(job_id)
    if not sjob:
        raise HTTPException(status_code=404, detail="Short maker job not found")

    if sjob["status"] != "completed" or not sjob.get("output_file"):
        raise HTTPException(status_code=404, detail="Output not ready")

    path = Path(sjob["output_file"])
    if not path.exists():
        raise HTTPException(status_code=404, detail="Output file missing")

    return FileResponse(
        path=str(path),
        filename=f"short_{sjob['filename']}",
        media_type="video/mp4",
    )


async def _run_short_maker(
    job_id: str,
    top_crop_dict: dict | None,
    bottom_crop_dict: dict | None,
    padding: int,
) -> None:
    """Background task: run the Short Maker FFmpeg pipeline."""
    sjob = _short_jobs[job_id]

    def log(msg: str):
        sjob["log_lines"].append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        logger.info("[Short {}] {}", job_id[:8], msg)

    try:
        from processors.short_maker import make_short, CropRegion

        video_path = Path(sjob["video_path"])
        output_dir = SHORTS_OUTPUT_DIR / job_id
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"short_{sjob['filename']}"

        # If output_path doesn't end with .mp4, fix it
        if output_path.suffix.lower() not in (".mp4",):
            output_path = output_path.with_suffix(".mp4")

        top_crop = CropRegion.from_dict(top_crop_dict) if top_crop_dict else None
        bottom_crop = CropRegion.from_dict(bottom_crop_dict) if bottom_crop_dict else None

        sjob["progress"] = 10
        log("Starting Short Maker...")

        result = await make_short(
            input_video=video_path,
            output_path=output_path,
            top_crop=top_crop,
            bottom_crop=bottom_crop,
            padding=padding,
            log_fn=log,
        )

        sjob["status"] = "completed"
        sjob["progress"] = 100
        sjob["output_file"] = str(result)
        log(f"✓ Short video created: {result.name}")

    except asyncio.CancelledError:
        sjob["status"] = "cancelled"
        log("Short maker cancelled")

    except Exception as exc:
        sjob["status"] = "failed"
        sjob["error"] = str(exc)
        log(f"✗ Error: {exc}")
        logger.exception("[Short {}] Failed", job_id[:8])


# ─── Short Maker: Use existing job video ─────────────────────────────────────

@app.post("/api/short-maker/from-job/{source_job_id}")
async def short_maker_from_job(source_job_id: str):
    """Create a Short Maker job from an existing pipeline job's video."""
    source_job = _jobs.get(source_job_id)
    if not source_job:
        raise HTTPException(status_code=404, detail="Source job not found")
    if not source_job.video_path:
        raise HTTPException(status_code=400, detail="Source job has no video")

    video_file = Path(source_job.video_path)
    if not video_file.exists():
        raise HTTPException(status_code=404, detail="Source video file not found on disk")

    job_id = uuid.uuid4().hex[:12]

    _short_jobs[job_id] = {
        "id": job_id,
        "filename": source_job.filename,
        "video_path": str(video_file),
        "status": "uploaded",
        "progress": 0,
        "output_file": None,
        "error": None,
        "created_at": time.time(),
        "log_lines": [],
        "source_job_id": source_job_id,
    }

    return {"job_id": job_id, "filename": source_job.filename}


# ═══════════════════════════════════════════════════════════════════════════════
# ALL IN — Workspace 04 (one-shot orchestrator)
# ═══════════════════════════════════════════════════════════════════════════════
#
# Chains Clip Finder + Auto-Subtitle + Short Maker into a single hands-off
# Job per design grilling Q1-Q15 + ADR-0002.

from web.services.all_in.models import (  # noqa: E402
    AllInJob, AllInJobStatus, AllInClipStatus, AspectRatio,
    CaptionPreset, DetectionMode,
)
from web.services.all_in.runner import (  # noqa: E402
    run_all_in_job as _run_all_in_job_impl,
    retry_clip as _retry_clip_impl,
)
from web.services.all_in.presets import list_preset_names  # noqa: E402

ALL_IN_DIR = Path("./output/all_in")
ALL_IN_DIR.mkdir(parents=True, exist_ok=True)

_all_in_jobs: dict[str, AllInJob] = {}
_all_in_tasks: dict[str, asyncio.Task] = {}


def _persist_all_in_job(job: AllInJob) -> None:
    """Save All In Job metadata for restart-survival."""
    job_dir = ALL_IN_DIR / job.id
    job_dir.mkdir(parents=True, exist_ok=True)
    try:
        with (job_dir / "job_meta.json").open("w", encoding="utf-8") as f:
            json.dump(
                job.model_dump(exclude={"log_lines", "transcript"}),
                f, ensure_ascii=False, indent=2,
            )
    except Exception as exc:
        logger.warning("[AllIn {}] persist failed: {}", job.id[:8], exc)


@app.on_event("startup")
async def restore_all_in_jobs() -> None:
    """Re-hydrate All In Jobs from disk on server start."""
    if not ALL_IN_DIR.exists():
        return
    restored = 0
    for job_dir in sorted(ALL_IN_DIR.iterdir(), reverse=True):
        if not job_dir.is_dir():
            continue
        meta_file = job_dir / "job_meta.json"
        if not meta_file.exists() or job_dir.name in _all_in_jobs:
            continue
        try:
            with meta_file.open("r", encoding="utf-8") as f:
                meta = json.load(f)
            valid_keys = set(AllInJob.model_fields.keys())
            filtered = {k: v for k, v in meta.items() if k in valid_keys}
            ai_job = AllInJob(**filtered)
            # Downgrade in-flight statuses to a safe terminal state.
            in_flight = {
                AllInJobStatus.QUEUED.value,
                AllInJobStatus.DOWNLOADING.value,
                AllInJobStatus.ANALYZING.value,
                AllInJobStatus.RENDERING.value,
            }
            if ai_job.status in in_flight:
                if ai_job.clips:
                    ai_job.status = AllInJobStatus.COMPLETED
                    ai_job.phase_label = (
                        f"Resumed from disk — {ai_job.done_count()} done, "
                        f"{ai_job.failed_count()} failed"
                    )
                else:
                    ai_job.status = AllInJobStatus.FAILED
                    ai_job.phase_label = "Server restarted before completion"
            _all_in_jobs[ai_job.id] = ai_job
            restored += 1
        except Exception as exc:
            logger.warning("[AllIn] restore {}: {}", job_dir.name, exc)
    if restored:
        logger.info("[AllIn] restored {} job(s)", restored)


class AllInRequest(BaseModel):
    url: str
    instructions: str = ""
    analysis_lang: str = "en"
    caption_lang: str = "en"
    aspect_ratio: str = "9:16"
    tighten_silence: bool = True
    speaker_tinting: bool = False
    auto_subtitle: bool = True
    caption_preset: str = "bold"
    # Advanced (mirrors Clip Finder)
    mode: str = "single-shot"
    enable_audio_signals: bool = True
    enable_chat_signals: bool = True
    start_offset: float = 0.0
    max_clips: int = 12
    # ADR-0003: Scoring Profile + Cut Strategies (optional, backward
    # compatible). Default scoring_profile=vtuber matches the legacy
    # ClipScore.total weights byte-for-byte. Empty cut_strategies =
    # legacy 1 base Moment → 1 Clip mapping (no fan-out).
    scoring_profile: str = "vtuber"
    cut_strategies: list[str] = []


@app.get("/api/all-in/presets")
async def list_all_in_presets():
    """Return the available caption preset names for the All In form."""
    return {"presets": list_preset_names()}


@app.post("/api/all-in/jobs")
async def create_all_in_job(req: AllInRequest):
    """Create and start a new All In Job."""
    if not req.url.strip():
        raise HTTPException(status_code=400, detail="URL is required")
    if not config.GEMINI_API_KEYS:
        raise HTTPException(status_code=400, detail="No GEMINI_API_KEY set in .env")

    # Validate enums up front so we fail with 400 rather than 500.
    try:
        AspectRatio(req.aspect_ratio)
        CaptionPreset(req.caption_preset)
        DetectionMode(req.mode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid enum value: {exc}")

    # ADR-0003 enums — validated separately so a bad scoring_profile
    # produces a clearer error than the generic "Invalid enum value".
    from web.services.all_in.models import (
        CutStrategyChoice as _CutStrategy,
        ScoringProfileChoice as _ScoringProfile,
    )
    try:
        scoring_profile = _ScoringProfile(req.scoring_profile)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid scoring_profile: {req.scoring_profile}",
        )
    try:
        cut_strategies = [_CutStrategy(s) for s in (req.cut_strategies or [])]
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid cut_strategies entry: {exc}",
        )

    job_id = uuid.uuid4().hex[:12]
    job = AllInJob(
        id=job_id,
        created_at=time.time(),
        url=req.url.strip(),
        instructions=req.instructions.strip(),
        analysis_lang=req.analysis_lang,
        caption_lang=req.caption_lang,
        aspect_ratio=req.aspect_ratio,
        tighten_silence=bool(req.tighten_silence),
        speaker_tinting=bool(req.speaker_tinting),
        auto_subtitle=bool(req.auto_subtitle),
        caption_preset=req.caption_preset,
        mode=req.mode,
        enable_audio_signals=bool(req.enable_audio_signals),
        enable_chat_signals=bool(req.enable_chat_signals),
        start_offset=max(0.0, req.start_offset),
        max_clips=max(1, min(50, req.max_clips)),
        scoring_profile=scoring_profile,
        cut_strategies=cut_strategies,
    )
    _all_in_jobs[job_id] = job
    _persist_all_in_job(job)

    task = asyncio.create_task(
        _run_all_in_job_impl(
            job, output_root=ALL_IN_DIR, gemini_keys=config.GEMINI_API_KEYS,
        )
    )

    def _cleanup(_t: asyncio.Task) -> None:
        if _all_in_tasks.get(job_id) is _t:
            _all_in_tasks.pop(job_id, None)

    _all_in_tasks[job_id] = task
    task.add_done_callback(_cleanup)

    return job.model_dump(exclude={"log_lines", "transcript"})


@app.get("/api/all-in/jobs")
async def list_all_in_jobs():
    jobs = sorted(_all_in_jobs.values(), key=lambda j: j.created_at, reverse=True)
    return [j.model_dump(exclude={"log_lines", "transcript"}) for j in jobs]


@app.get("/api/all-in/jobs/{job_id}")
async def get_all_in_job(job_id: str):
    job = _all_in_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="All In job not found")
    return job.model_dump(exclude={"log_lines", "transcript"})


@app.get("/api/all-in/jobs/{job_id}/log")
async def stream_all_in_log(job_id: str):
    """SSE log stream for an All In Job."""
    if job_id not in _all_in_jobs:
        raise HTTPException(status_code=404, detail="All In job not found")
    terminal = {
        AllInJobStatus.COMPLETED.value,
        AllInJobStatus.FAILED.value,
        AllInJobStatus.CANCELLED.value,
    }

    async def event_generator() -> AsyncGenerator[str, None]:
        sent = 0
        while True:
            j = _all_in_jobs.get(job_id)
            if not j:
                break
            for line in j.log_lines[sent:]:
                yield f"data: {json.dumps({'line': line})}\n\n"
                sent += 1
            if j.status in terminal:
                yield f"data: {json.dumps({'done': True, 'status': j.status})}\n\n"
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _resolve_all_in_clip(job_id: str, clip_idx: int) -> Path:
    job = _all_in_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="All In job not found")
    if clip_idx < 0 or clip_idx >= len(job.clips):
        raise HTTPException(status_code=404, detail="Clip index out of range")
    clip = job.clips[clip_idx]
    if not clip.clip_file:
        raise HTTPException(status_code=404, detail="Clip not rendered yet")
    path = Path(clip.clip_file)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Clip file missing on disk")
    return path


@app.get("/api/all-in/jobs/{job_id}/clips/{clip_idx}/stream")
async def stream_all_in_clip(job_id: str, clip_idx: int):
    path = _resolve_all_in_clip(job_id, clip_idx)
    return FileResponse(
        path=str(path),
        media_type="video/mp4",
        headers={"Accept-Ranges": "bytes"},
    )


@app.get("/api/all-in/jobs/{job_id}/clips/{clip_idx}/download")
async def download_all_in_clip(job_id: str, clip_idx: int):
    path = _resolve_all_in_clip(job_id, clip_idx)
    return FileResponse(
        path=str(path), filename=path.name, media_type="video/mp4",
    )


@app.get("/api/all-in/jobs/{job_id}/clips/{clip_idx}/sidecar")
async def get_all_in_clip_sidecar(job_id: str, clip_idx: int):
    """Return the upload-ready Clip Sidecar metadata for a finished Clip.

    Reads ``{clip}.metadata.json`` written by the runner's Stage 5.
    Returns 404 if the Clip is not yet rendered or the sidecar is
    missing (the file is best-effort — Gemini outage during render
    leaves no sidecar but the Clip is still valid).
    """
    from processors.clip_finder.clip_sidecar import read as _read_sidecar

    clip_path = _resolve_all_in_clip(job_id, clip_idx)
    sidecar = _read_sidecar(clip_path)
    if sidecar is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "Sidecar metadata not available for this clip "
                "(Gemini may have been unreachable during render)"
            ),
        )
    return sidecar.to_dict()


@app.post("/api/all-in/jobs/{job_id}/clips/{clip_idx}/retry")
async def retry_all_in_clip(job_id: str, clip_idx: int):
    """Retry a single failed Clip without re-downloading the source (Q10)."""
    job = _all_in_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="All In job not found")
    if clip_idx < 0 or clip_idx >= len(job.clips):
        raise HTTPException(status_code=404, detail="Clip index out of range")

    task = _all_in_tasks.get(job_id)
    if task and not task.done():
        raise HTTPException(status_code=409, detail="Job still running")

    async def _retry_runner():
        try:
            await _retry_clip_impl(job, clip_idx, output_root=ALL_IN_DIR)
        except (IndexError, RuntimeError) as exc:
            job.log_lines.append(f"[retry] {exc}")

    new_task = asyncio.create_task(_retry_runner())
    _all_in_tasks[job_id] = new_task

    def _cleanup(_t: asyncio.Task) -> None:
        if _all_in_tasks.get(job_id) is _t:
            _all_in_tasks.pop(job_id, None)

    new_task.add_done_callback(_cleanup)
    return {"job_id": job_id, "clip_idx": clip_idx, "status": "retrying"}


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

@app.get("/", response_class=HTMLResponse)
async def page_home(request: Request):
    return templates.TemplateResponse(request, "pages/home.html", {"active": "home"})


@app.get("/auto-subtitle", response_class=HTMLResponse)
async def page_auto_subtitle(request: Request):
    return templates.TemplateResponse(
        request, "pages/auto_subtitle.html", {"active": "subtitle"},
    )


@app.get("/clip-finder", response_class=HTMLResponse)
async def page_clip_finder(request: Request):
    return templates.TemplateResponse(
        request, "pages/clip_finder.html", {"active": "clipfinder"},
    )


@app.get("/short-maker", response_class=HTMLResponse)
async def page_short_maker(request: Request):
    return templates.TemplateResponse(
        request, "pages/short_maker.html", {"active": "shortmaker"},
    )


@app.get("/all-in", response_class=HTMLResponse)
async def page_all_in(request: Request):
    return templates.TemplateResponse(
        request, "pages/all_in.html", {"active": "allin"},
    )


@app.get("/editor", response_class=HTMLResponse)
@app.get("/editor/{job_id}", response_class=HTMLResponse)
async def page_editor(request: Request, job_id: str | None = None):
    return templates.TemplateResponse(
        request,
        "pages/editor.html",
        {"active": "editor", "job_id": job_id},
    )


# ─── Mount Static Files (must be last) ───────────────────────────────────────

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
