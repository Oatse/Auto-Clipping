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

import torch
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
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

UPLOADS_DIR = Path("./output/uploads")
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

OUTPUT_ROOT = Path("./output")
OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)


def _is_job_id(name: str) -> bool:
    """Return True if name looks like a 12-char hex job ID."""
    return bool(re.match(r'^[0-9a-f]{12}$', name))


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

class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


PHASE_LABELS = {
    1: "Transcription & Diarization",
    2: "Translation",
    3: "Subtitle Rendering",
    4: "Final Muxing",
}


class Job(BaseModel):
    id: str
    filename: str
    target_language: str
    status: JobStatus = JobStatus.QUEUED
    current_phase: int = 0
    total_phases: int = 4
    progress_pct: float = 0.0
    phase_label: str = "Queued"
    output_file: str | None = None
    error: str | None = None
    created_at: float = 0.0
    started_at: float | None = None
    completed_at: float | None = None
    log_lines: list[str] = []
    video_path: str | None = None          # Path video yang diupload
    transcript_path: str | None = None     # Path file JSON transkripsi hasil phase 1
    transcribe_only: bool = False          # Jika True, pipeline berhenti setelah phase 1
    num_speakers: int | None = None        # Manual speaker count override (None = auto)
    speaker_detection: bool = True         # False = skip gap detection, semua SPEAKER_00
    whisper_model: str = "large-v2"        # Whisper model key from config.WHISPER_MODELS

    class Config:
        use_enum_values = True


# In-memory job store (replace with Redis/DB for production)
_jobs: dict[str, Job] = {}
_job_tasks: dict[str, asyncio.Task] = {}


# ─── System Info ──────────────────────────────────────────────────────────────

@app.get("/api/system")
async def get_system_info():
    cuda_available = torch.cuda.is_available()
    gpu_name = torch.cuda.get_device_name(0) if cuda_available else None
    return {
        "cuda_available": cuda_available,
        "gpu_name": gpu_name,
        "torch_version": torch.__version__,
        "python_version": sys.version.split()[0],
        "packages": {
            "whisperx": _check_package("whisperx"),
            "pycaps": _check_package("pycaps"),
            "ffmpeg": _check_ffmpeg(),
        },
        "env": {
            "hf_token_set": bool(config.HF_TOKEN),
            "elevenlabs_key_set": bool(config.ELEVENLABS_API_KEY),
            "gemini_keys_set": bool(config.GEMINI_API_KEYS),
        },
        "whisper_models": {
            key: {
                "label": m["label"],
                "description": m.get("description", ""),
                "type": m.get("type", "whisperx"),
            }
            for key, m in config.WHISPER_MODELS.items()
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
    task = _job_tasks.get(job_id)
    if task and not task.done():
        task.cancel()
    del _jobs[job_id]
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
    whisper_model: str = Form("large-v2"),
):
    # Validate file type
    if not video.filename.lower().endswith((".mp4", ".mov", ".mkv", ".avi")):
        raise HTTPException(status_code=400, detail="Only video files are accepted (.mp4, .mov, .mkv, .avi)")

    # Validate num_speakers if provided
    if num_speakers is not None and not (1 <= num_speakers <= 6):
        raise HTTPException(status_code=400, detail="num_speakers must be between 1 and 6")

    job_id = uuid.uuid4().hex[:12]
    upload_path = UPLOADS_DIR / f"{job_id}_{video.filename}"

    # Save uploaded file
    with upload_path.open("wb") as f:
        content = await video.read()
        f.write(content)

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
        whisper_model=whisper_model,
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
    _job_tasks[job_id] = task

    return job.model_dump(exclude={"log_lines"})


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


@app.get("/api/jobs/{job_id}/transcript/original")
async def get_original_transcript(job_id: str):
    """Return the original ElevenLabs transcript (before Gemini regrouping/translation)."""
    job = _get_job_or_404(job_id)

    output_dir = Path("./output") / job_id
    original_file = output_dir / "phase1_transcription" / "elevenlabs_original_transcript.json"

    if not original_file.exists():
        raise HTTPException(
            status_code=404,
            detail="Original ElevenLabs transcript not available for this job.",
        )

    with original_file.open("r", encoding="utf-8") as f:
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
    _job_tasks[job_id] = task

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
    """
    Jalankan HANYA Phase 1 (WhisperX transcription).
    Pipeline berhenti disini — user akan review di preview screen sebelum render.
    """
    job = _jobs[job_id]
    job.status = JobStatus.RUNNING
    job.started_at = time.time()

    output_dir = Path("./output") / job_id

    def log(msg: str):
        job.log_lines.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        logger.info("[Job {}] {}", job_id[:8], msg)

    def set_phase(phase: int):
        job.current_phase = phase
        job.phase_label = PHASE_LABELS.get(phase, f"Phase {phase}")
        job.progress_pct = round((phase - 1) / 4 * 100, 1)
        log(f"▶ Phase {phase}/4: {job.phase_label}")

    try:
        from main import VideoSubtitlePipeline

        log(f"Memulai transkripsi: {video_path.name}")
        if job.num_speakers:
            log(f"Speaker count override: {job.num_speakers} speaker(s)")
        if not job.speaker_detection:
            log("Speaker detection: OFF (single-speaker mode)")

        # Log which whisper model is being used
        model_key = job.whisper_model or "large-v2"
        model_info = config.WHISPER_MODELS.get(model_key, {})
        model_type = model_info.get("type", "whisperx")
        log(f"Model: {model_info.get('label', model_key)}")

        set_phase(1)

        if model_type == "elevenlabs":
            # ── ElevenLabs Speech-to-Text path ──
            from processors.elevenlabs_stt import ElevenLabsSTTProcessor

            if not config.ELEVENLABS_API_KEYS:
                raise ValueError("ELEVENLABS_API_KEY is not set in .env")

            log("Using ElevenLabs Speech-to-Text API...")
            el_processor = ElevenLabsSTTProcessor()
            segments, _ = await el_processor.transcribe(
                video_path=video_path,
                output_dir=output_dir / "phase1_transcription",
                speaker_detection=job.speaker_detection,
                num_speakers=job.num_speakers,
            )
            log(f"✓ ElevenLabs transkripsi selesai: {len(segments)} segmen (bahasa asal)")

            # ── Save original ElevenLabs transcript before Gemini regrouping ──
            el_original_dir = output_dir / "phase1_transcription"
            el_original_dir.mkdir(parents=True, exist_ok=True)
            el_original_path = el_original_dir / "elevenlabs_original_transcript.json"
            el_original_data = []
            for seg in segments:
                seg_d = {
                    "start": seg.start,
                    "end": seg.end,
                    "text": seg.text,
                    "speaker": getattr(seg, "speaker", "SPEAKER_00"),
                }
                if hasattr(seg, "words") and seg.words:
                    seg_d["words"] = [
                        {"word": getattr(w, "word", ""), "start": getattr(w, "start", 0), "end": getattr(w, "end", 0)}
                        for w in seg.words
                    ]
                el_original_data.append(seg_d)
            with el_original_path.open("w", encoding="utf-8") as f:
                json.dump({"segments": el_original_data}, f, ensure_ascii=False, indent=2)
            log(f"✓ ElevenLabs original transcript saved: {el_original_path.name}")

            # ── Auto-translate via Gemini if target language differs ──
            # ElevenLabs transcribes in the original language, so we translate
            # to the selected target language using Gemini.
            if target_language and config.GEMINI_API_KEYS:
                log(f"Auto-translating to '{target_language}' via Gemini...")
                from processors.translator import TranslatorProcessor
                translator = TranslatorProcessor(target_language=target_language)
                segments, _ = await translator.translate(
                    segments=segments,
                    output_dir=output_dir / "phase2_translation",
                    regroup=True,
                )
                log(f"✓ Auto-translate selesai: {len(segments)} segmen → '{target_language}'")
            elif not config.GEMINI_API_KEYS:
                log("⚠ No GEMINI_API_KEYS configured — skipping auto-translate")

        else:
            # ── Standard WhisperX / faster-whisper path ──
            pipeline = VideoSubtitlePipeline(
                input_video=video_path,
                output_dir=output_dir,
                target_language=target_language,
            )

            segments, _ = await pipeline.transcriber.transcribe(
                video_path=video_path,
                output_dir=output_dir / "phase1_transcription",
                num_speakers=job.num_speakers,
                speaker_detection=job.speaker_detection,
                model_key=model_key,
            )
            log(f"✓ Transkripsi selesai: {len(segments)} segmen ditemukan")

        # Simpan transkripsi ke file JSON
        transcript_output_dir = output_dir / "phase1_transcription"
        transcript_output_dir.mkdir(parents=True, exist_ok=True)
        transcript_file = transcript_output_dir / "source_transcript.json"

        segments_data = []
        for seg in segments:
            seg_dict = {
                "start": seg.start,
                "end": seg.end,
                "text": seg.text,
                "speaker": getattr(seg, "speaker", "SPEAKER_00"),
            }
            if hasattr(seg, "words") and seg.words:
                seg_dict["words"] = [
                    {
                        "word": getattr(w, "word", getattr(w, "text", str(w))),
                        "start": getattr(w, "start", 0),
                        "end": getattr(w, "end", 0),
                    }
                    for w in seg.words
                ]
            segments_data.append(seg_dict)

        with transcript_file.open("w", encoding="utf-8") as f:
            json.dump({"segments": segments_data}, f, ensure_ascii=False, indent=2)

        job.transcript_path = str(transcript_file)
        job.status = JobStatus.COMPLETED
        job.current_phase = 1
        job.progress_pct = 25.0
        job.phase_label = "Transcription complete — Ready for preview"
        job.completed_at = time.time()
        elapsed = round(job.completed_at - job.started_at, 1)
        log(f"✓ Transkripsi selesai dalam {elapsed}s → Siap untuk preview")

        # ── Persist metadata so job survives server restarts ──
        try:
            meta_path = output_dir / "job_meta.json"
            output_dir.mkdir(parents=True, exist_ok=True)
            with meta_path.open("w", encoding="utf-8") as f:
                json.dump(
                    job.model_dump(exclude={"log_lines"}),
                    f, ensure_ascii=False, indent=2
                )
        except Exception as meta_exc:
            logger.warning("[Job {}] Could not save job_meta.json: {}", job_id[:8], meta_exc)

    except asyncio.CancelledError:
        job.status = JobStatus.CANCELLED
        job.phase_label = "Cancelled"
        log("Job dibatalkan")

    except Exception as exc:
        job.status = JobStatus.FAILED
        job.phase_label = "Failed"
        job.error = str(exc)
        log(f"✗ Error: {exc}")
        logger.exception("[Job {}] Transcription failed", job_id[:8])


def _sync_segment_words_with_text(seg) -> None:
    """Sync a segment's word-level entries with its (possibly edited) text.

    When the user edits the segment text in the preview (or Gemini refines it),
    the `text` field changes but the `words` list still contains the original
    ElevenLabs/WhisperX words.  This causes the Pycaps word-pop renderer to
    display the *old* text.

    Strategy:
    - Split `seg.text` into new words.
    - If the word count matches, just update each word's text in-place
      (preserving the original timestamps).
    - If the word count differs, redistribute the segment's time span
      proportionally across the new words.
    """
    from models.transcript import WordTimestamp

    new_words = seg.text.strip().split()
    if not new_words:
        return

    old_words = seg.words or []

    # Check if text already matches — no sync needed
    if len(old_words) == len(new_words):
        all_match = all(
            ow.word.strip().lower() == nw.strip().lower()
            for ow, nw in zip(old_words, new_words)
        )
        if all_match:
            return

    if len(old_words) == len(new_words):
        # Same word count: just update the word text, keep timestamps
        for ow, nw in zip(old_words, new_words):
            ow.word = nw
    else:
        # Different word count: redistribute timestamps proportionally
        seg_start = seg.start
        seg_end = seg.end
        seg_duration = seg_end - seg_start
        n = len(new_words)
        word_dur = seg_duration / n if n > 0 else 0

        new_word_list = []
        for i, w in enumerate(new_words):
            ws = round(seg_start + i * word_dur, 3)
            we = round(seg_start + (i + 1) * word_dur, 3)
            new_word_list.append(WordTimestamp(word=w, start=ws, end=we))
        seg.words = new_word_list


async def _run_render_pipeline(
    job_id: str,
    video_path: Path,
    target_language: str,
    style_config: dict,
) -> None:
    """
    Jalankan pipeline Phase 2-4 menggunakan transkripsi yang sudah ada.
    Dipanggil setelah user mengatur subtitle style di preview screen.
    """
    job = _jobs[job_id]
    job.status = JobStatus.RUNNING
    job.started_at = time.time()

    output_dir = Path("./output") / job_id

    def log(msg: str):
        job.log_lines.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        logger.info("[Job {}] {}", job_id[:8], msg)

    def set_phase(phase: int):
        job.current_phase = phase
        job.phase_label = PHASE_LABELS.get(phase, f"Phase {phase}")
        job.progress_pct = round((phase - 1) / 4 * 100, 1)
        log(f"▶ Phase {phase}/4: {job.phase_label}")

    try:
        from main import VideoSubtitlePipeline

        log(f"Memulai render pipeline: {video_path.name}")
        if style_config:
            log(f"Style: font={style_config.get('fontFamily','default')}, anim={style_config.get('animStyle','word-pop')}")

        pipeline = VideoSubtitlePipeline(
            input_video=video_path,
            output_dir=output_dir,
            target_language=target_language,
        )

        from models.transcript import TranscriptSegment

        # ── Use user-edited transcript from frontend if available ────────
        # The frontend sends the user's edited transcript (with any text,
        # timing, speaker reassignment, merge/split changes) inside
        # style_config["transcript"].  We prefer this over the cached
        # Phase-1 transcript so every edit the user made in the preview
        # screen (including per-speaker colour assignments) is faithfully
        # reflected in the rendered video.
        user_transcript = style_config.get("transcript") if style_config else None
        transcript_source = style_config.get("transcriptSource", "refined") if style_config else "refined"

        if user_transcript and isinstance(user_transcript, list) and len(user_transcript) > 0:
            source_label = "original ElevenLabs" if transcript_source == "original" else "refined (user-edited)"
            log(f"✓ Menggunakan transkrip {source_label} dari preview")
            job.current_phase = 1
            job.phase_label = f"Transcript ({source_label})"
            job.progress_pct = 25.0

            segments = []
            for seg_dict in user_transcript:
                if isinstance(seg_dict, dict):
                    segments.append(TranscriptSegment.from_dict(seg_dict))

            # ── Sync word-level text with segment text ────────────────────
            # The segment `text` field may have been refined by Gemini or
            # edited by the user, but the `words` array still contains the
            # original ElevenLabs/WhisperX word texts.  This causes the
            # Pycaps renderer (which reads word-level data) to show the old
            # text instead of the refined/edited text.  Re-split the
            # segment text into words and re-assign them to the existing
            # word timestamps so both word-level and segment-level text
            # are in sync.
            for seg in segments:
                _sync_segment_words_with_text(seg)

            log(f"✓ Dimuat {len(segments)} segmen dari preview")

            # Skip Phase 2 (translation) — the user already edited the
            # text in the preview, so re-translating would overwrite their
            # changes.
            set_phase(2)
            translated_segments = segments
            log(f"✓ Terjemahan di-skip (menggunakan teks dari preview)")
        else:
            # Fallback: load from Phase-1 cache and translate
            transcript_file = output_dir / "phase1_transcription" / "source_transcript.json"
            if transcript_file.exists():
                log("✓ Menggunakan cache Phase 1 (skip re-transcribe)")
                job.current_phase = 1
                job.phase_label = "Transcription (cached)"
                job.progress_pct = 25.0

                with transcript_file.open("r", encoding="utf-8") as f:
                    raw = json.load(f)

                segments = [
                    TranscriptSegment.from_dict(seg)
                    for seg in raw.get("segments", [])
                ]
                log(f"✓ Dimuat {len(segments)} segmen dari cache")
            else:
                log("Tidak ada cache Phase 1, menjalankan transkripsi...")
                set_phase(1)
                model_key = job.whisper_model or "large-v2"
                segments, _ = await pipeline.transcriber.transcribe(
                    video_path=video_path,
                    output_dir=output_dir / "phase1_transcription",
                    num_speakers=job.num_speakers,
                    speaker_detection=job.speaker_detection,
                    model_key=model_key,
                )
                log(f"✓ Phase 1 selesai: {len(segments)} segmen")

            # Phase 2 — Translation
            set_phase(2)
            translated_segments, _ = await pipeline.translator.translate(
                segments=segments,
                output_dir=output_dir / "phase2_translation",
            )
            log(f"✓ Terjemahan: {len(translated_segments)} segmen ke '{target_language}'")

        # Phase 3 — Subtitle Rendering
        set_phase(3)
        pycaps_json = pipeline.subtitle_renderer.build_pycaps_transcript(
            segments=translated_segments,
            output_dir=output_dir / "phase3_subtitles",
        )
        subtitled_video = await asyncio.to_thread(
            pipeline.subtitle_renderer.render,
            video_path=video_path,
            pycaps_transcript=pycaps_json,
            output_path=output_dir / "phase3_subtitles" / "subtitled.mp4",
            style_config=style_config,
            segments=translated_segments,
        )
        log("✓ Subtitle rendering selesai")

        # Phase 4 — Final Mux
        set_phase(4)
        stem = video_path.stem
        final_output = await pipeline.muxer.mux(
            video_path=subtitled_video,
            output_path=output_dir / f"{stem}_subtitled_{target_language}.mp4",
        )

        job.status = JobStatus.COMPLETED
        job.progress_pct = 100.0
        job.phase_label = "Completed"
        job.output_file = str(final_output)
        job.completed_at = time.time()
        elapsed = round(job.completed_at - job.started_at, 1)
        log(f"✓ Render selesai dalam {elapsed}s → {final_output.name}")

    except asyncio.CancelledError:
        job.status = JobStatus.CANCELLED
        job.phase_label = "Cancelled"
        log("Render dibatalkan")

    except Exception as exc:
        job.status = JobStatus.FAILED
        job.phase_label = "Failed"
        job.error = str(exc)
        log(f"✗ Error: {exc}")
        logger.exception("[Job {}] Render pipeline failed", job_id[:8])


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
    status: str = "queued"          # queued|transcribing|analyzing|analyzed|downloading|completed|failed
    progress_pct: float = 0.0
    phase_label: str = "Queued"
    error: str | None = None
    created_at: float = 0.0
    video_title: str | None = None
    clips: list[dict] = []          # [{start, end, title, reason}]
    clip_files: list[str] = []      # file paths of cut clips
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
    start_offset: float = 0.0  # Skip first N seconds (for livestreams)


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
            # Attach title/reason from job data if available
            if cf_job and i < len(cf_job.clips):
                clip_info["title"] = cf_job.clips[i].get("title", "")
                clip_info["reason"] = cf_job.clips[i].get("reason", "")
                clip_info["start"] = cf_job.clips[i].get("start", 0)
                clip_info["end"] = cf_job.clips[i].get("end", 0)
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
    whisper_model: str = Form("large-v2"),
    double_check: bool = Form(True),
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
        whisper_model=whisper_model,
    )
    _jobs[job_id] = job

    # Check for adjacent auto-sub file for double-check merge
    autosub_path = _find_autosub_for_clip(clip_file) if double_check else None

    if autosub_path:
        task = asyncio.create_task(
            _run_double_check_transcription(
                job_id, clip_file, target_language, autosub_path
            )
        )
    else:
        task = asyncio.create_task(
            _run_transcription_only(job_id, clip_file, target_language)
        )
    _job_tasks[job_id] = task

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

    job_id = uuid.uuid4().hex[:12]
    job = ClipFinderJob(
        id=job_id,
        url=req.url.strip(),
        instructions=req.instructions.strip() if req.instructions.strip() else "",
        lang=req.lang,
        start_offset=max(0.0, req.start_offset),
        created_at=time.time(),
    )
    _cf_jobs[job_id] = job

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
    """Phase 1: Extract transcript from YouTube via yt-dlp auto-subs + Gemini AI analysis."""
    job = _cf_jobs[job_id]
    job_dir = CLIP_FINDER_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    def log(msg: str):
        job.log_lines.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        logger.info("[ClipFinder {}] {}", job_id[:8], msg)

    try:
        from processors.clip_finder import ClipFinder, ClipFinderError

        cf = ClipFinder()

        # ── Step 1: Extract transcript via yt-dlp (no video download) ──
        job.status = "transcribing"
        job.phase_label = "Extracting transcript..."
        job.progress_pct = 15.0
        log("Step 1/2: Extracting transcript from YouTube (no video download)...")

        transcript = await cf.extract_subtitles(
            url=job.url,
            output_dir=job_dir / "subs",
            lang=job.lang,
            log_fn=log,
        )

        if not transcript or len(transcript) == 0:
            log("No subtitles found after trying all strategies.")
            job.status = "failed"
            job.phase_label = "Failed — No subtitles available"
            job.error = (
                "No subtitles found for this video. "
                "Tried auto-generated and manual subtitles in multiple languages. "
                "The video may not have any captions available."
            )
            return

        log(f"Transcript extracted: {len(transcript)} segments")

        # ── Apply start offset (for livestreams with waiting time) ──
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
                return

        # ── Step 2: Gemini AI analysis ──
        job.status = "analyzing"
        job.phase_label = "AI analyzing transcript..."
        job.progress_pct = 45.0
        log(f"Step 2/2: Analyzing with Gemini AI ({len(gemini_keys)} key(s) available)...")

        clips = await cf.find_clips_with_gemini(
            transcript=transcript,
            instructions=job.instructions,
            api_keys=gemini_keys,
            log_fn=log,
        )

        if not clips:
            log("No clips matched your instructions.")
            job.status = "analyzed"
            job.phase_label = "Analysis complete — No matching clips found"
            job.progress_pct = 100.0
            job.clips = []
            return

        job.clips = clips
        job.transcript = transcript  # Preserve for Phase 2 auto-sub slicing (double-check)

        # ── Phase 1 done — clips found, ready for user to review & download ──
        job.status = "analyzed"
        job.phase_label = f"Found {len(clips)} clips — Ready to download"
        job.progress_pct = 100.0
        log(f"Analysis complete! Found {len(clips)} clips matching your instructions.")
        log("Click 'Download Clips' to download the video sections.")

    except Exception as exc:
        job.status = "failed"
        job.phase_label = "Failed"
        job.error = str(exc)
        log(f"Error: {exc}")
        logger.exception("[ClipFinder {}] Phase 1 failed", job_id[:8])


async def _run_clip_download(job_id: str) -> None:
    """Phase 2: Download only the relevant clip sections using yt-dlp --download-sections."""
    job = _cf_jobs[job_id]
    job_dir = CLIP_FINDER_DIR / job_id

    def log(msg: str):
        job.log_lines.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        logger.info("[ClipFinder {}] {}", job_id[:8], msg)

    try:
        from processors.clip_finder import ClipFinder

        cf = ClipFinder()

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

    except Exception as exc:
        job.status = "failed"
        job.phase_label = "Download failed"
        job.error = str(exc)
        log(f"Error: {exc}")
        logger.exception("[ClipFinder {}] Download failed", job_id[:8])


async def _run_single_clip_download(job_id: str, clip_idx: int) -> None:
    """Download a single clip section by index."""
    job = _cf_jobs[job_id]
    job_dir = CLIP_FINDER_DIR / job_id

    def log(msg: str):
        job.log_lines.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        logger.info("[ClipFinder {}] {}", job_id[:8], msg)

    try:
        from processors.clip_finder import ClipFinder

        cf = ClipFinder()
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

    except Exception as exc:
        log(f"Error downloading clip {clip_idx + 1}: {exc}")
        logger.exception("[ClipFinder {}] Single clip download failed", job_id[:8])


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


async def _run_double_check_transcription(
    job_id: str,
    video_path: Path,
    target_language: str,
    autosub_path: Path,
) -> None:
    """Run WhisperX transcription, then merge with auto-subs using Double-Check."""
    job = _jobs[job_id]
    job.status = JobStatus.RUNNING
    job.started_at = time.time()

    output_dir = Path("./output") / job_id

    def log(msg: str):
        job.log_lines.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        logger.info("[Job {}] {}", job_id[:8], msg)

    def set_phase(phase: int):
        job.current_phase = phase
        job.phase_label = PHASE_LABELS.get(phase, f"Phase {phase}")
        job.progress_pct = round((phase - 1) / 4 * 100, 1)

    try:
        from main import VideoSubtitlePipeline
        from processors.double_check import DoubleCheckMerger
        from models.transcript import sanitize_timestamps

        log(f"Starting Double-Check transcription: {video_path.name}")

        # ── Step 1: Load auto-subs ──
        log("Step 1/4: Loading auto-sub transcript...")
        autosub_segments = DoubleCheckMerger.load_autosub_json(autosub_path)
        log(f"  Loaded {len(autosub_segments)} auto-sub segments from {autosub_path.name}")

        # ── Step 2: Run WhisperX ──
        log("Step 2/4: Running WhisperX transcription...")
        set_phase(1)

        model_key = job.whisper_model or "large-v2"
        model_info = config.WHISPER_MODELS.get(model_key, {})
        log(f"  Whisper model: {model_info.get('label', model_key)}")

        pipeline = VideoSubtitlePipeline(
            input_video=video_path,
            output_dir=output_dir,
            target_language=target_language,
        )

        whisperx_segments, _ = await pipeline.transcriber.transcribe(
            video_path=video_path,
            output_dir=output_dir / "phase1_transcription",
            num_speakers=job.num_speakers,
            speaker_detection=job.speaker_detection,
            model_key=model_key,
        )
        log(f"  WhisperX produced {len(whisperx_segments)} segments")

        # ── Step 3: Double-Check merge ──
        log("Step 3/4: Running Double-Check merge...")
        merger = DoubleCheckMerger()
        merged_segments, stats = merger.merge(
            autosub_segments=autosub_segments,
            whisperx_segments=whisperx_segments,
        )

        log(
            f"  Double-Check complete: {stats.total_words} words — "
            f"{stats.both_agree} agreed, {stats.autosub_only} autosub-only, "
            f"{stats.whisperx_only} whisperx-only, "
            f"{stats.autosub_preferred} autosub-preferred, "
            f"{stats.whisperx_preferred} whisperx-preferred, "
            f"{stats.interpolated} interpolated"
        )

        # ── Step 4: LLM review (optional) ──
        debug_dir = output_dir / "double_check_debug"
        if config.GEMINI_API_KEYS:
            log("Step 4/4: LLM reviewing conflict words...")
            merged_segments, llm_corrected = await merger.llm_review(
                merged_segments=merged_segments,
                api_keys=config.GEMINI_API_KEYS,
                log_fn=log,
                debug_dir=debug_dir,
            )
            stats.llm_corrected = llm_corrected
            log(f"  LLM corrected {llm_corrected} words")
        else:
            log("Step 4/4: Skipped LLM review (no Gemini API keys)")

        # ── Sanitize merged timestamps ──
        merged_segments = sanitize_timestamps(merged_segments)

        # ── Save merged transcript ──
        transcript_output_dir = output_dir / "phase1_transcription"
        transcript_output_dir.mkdir(parents=True, exist_ok=True)
        transcript_file = transcript_output_dir / "source_transcript.json"

        segments_data = []
        for seg in merged_segments:
            seg_dict = {
                "start": seg.start,
                "end": seg.end,
                "text": seg.text,
                "speaker": getattr(seg, "speaker", "SPEAKER_00"),
            }
            if hasattr(seg, "words") and seg.words:
                seg_dict["words"] = [
                    {
                        "word": getattr(w, "word", getattr(w, "text", str(w))),
                        "start": getattr(w, "start", 0),
                        "end": getattr(w, "end", 0),
                        "score": getattr(w, "score", 0),
                        "source": getattr(w, "source", ""),
                    }
                    for w in seg.words
                ]
            segments_data.append(seg_dict)

        with transcript_file.open("w", encoding="utf-8") as f:
            json.dump({"segments": segments_data}, f, ensure_ascii=False, indent=2)

        # ── Save debug info ──
        debug_dir.mkdir(parents=True, exist_ok=True)

        with (debug_dir / "autosub_input.json").open("w", encoding="utf-8") as f:
            json.dump(autosub_segments, f, ensure_ascii=False, indent=2)
        with (debug_dir / "whisperx_input.json").open("w", encoding="utf-8") as f:
            whisperx_data = [s.to_dict() for s in whisperx_segments]
            json.dump(whisperx_data, f, ensure_ascii=False, indent=2)
        with (debug_dir / "merge_stats.json").open("w", encoding="utf-8") as f:
            import dataclasses as _dc
            json.dump(_dc.asdict(stats), f, indent=2)

        log(f"  Debug files saved to {debug_dir}")

        # ── Done ──
        job.transcript_path = str(transcript_file)
        job.status = JobStatus.COMPLETED
        job.current_phase = 1
        job.progress_pct = 25.0
        job.phase_label = "Double-Check transcription complete — Ready for preview"
        job.completed_at = time.time()
        elapsed = round(job.completed_at - job.started_at, 1)
        log(f"Double-Check transcription complete in {elapsed}s → Siap untuk preview")

        # ── Persist metadata ──
        try:
            meta_path = output_dir / "job_meta.json"
            output_dir.mkdir(parents=True, exist_ok=True)
            with meta_path.open("w", encoding="utf-8") as f:
                json.dump(
                    job.model_dump(exclude={"log_lines"}),
                    f, ensure_ascii=False, indent=2,
                )
        except Exception as meta_exc:
            logger.warning("[Job {}] Could not save job_meta.json: {}", job_id[:8], meta_exc)

    except asyncio.CancelledError:
        job.status = JobStatus.CANCELLED
        job.phase_label = "Cancelled"
        log("Job dibatalkan")

    except Exception as exc:
        job.status = JobStatus.FAILED
        job.phase_label = "Failed"
        job.error = str(exc)
        log(f"Error: {exc}")
        logger.exception("[Job {}] Double-check transcription failed", job_id[:8])


# ─── Mount Static Files (must be last) ───────────────────────────────────────

app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
