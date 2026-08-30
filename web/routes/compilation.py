"""
web/routes/compilation.py — Compilation / Premiere handoff HTTP surface.

One URL in, one Premiere-ready timeline out. Unlike Clip Finder — which
publishes individual clips — this workspace keeps every moment above a
quality bar and hands the whole set over as a master file plus an FCPXML
timeline the editor assembles into one long video.

Endpoints:
  - POST /api/compilation/jobs             start a run
  - GET  /api/compilation/jobs             list runs
  - GET  /api/compilation/jobs/{id}        poll one run
  - GET  /api/compilation/jobs/{id}/log    SSE log stream
  - GET  /api/compilation/jobs/{id}/fcpxml download the timeline

State lives in ``job_state.comp_jobs`` / ``comp_tasks``.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path
from typing import AsyncGenerator

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from loguru import logger
from pydantic import BaseModel

import config
from processors.premiere import build_compilation
from web.services import job_state

router = APIRouter()

# Detection backends accepted for the moment-finding model. Mirrors the
# Clip Finder list so a user's mental model carries between workspaces.
VALID_MODELS = {
    "gemini",
    "claude-opus-4.6",
    "kiro-opus-4.7",
    "kiro-sonnet-4.6",
    "kiro-auto",
    "codex-gpt-5.5",
    "codex-gpt-5.4",
}


# ─── Domain model ────────────────────────────────────────────────────────────


class CompilationJob(BaseModel):
    """State of one compilation run."""

    id: str
    url: str
    instructions: str = ""
    lang: str = "ja"
    start_offset: float = 0.0
    model: str = "gemini"
    project_name: str = ""
    threshold: float | None = None
    enable_chat_signals: bool = True
    enable_audio_signals: bool = True

    status: str = "pending"          # pending | running | completed | failed
    phase_label: str = "Queued"
    created_at: float = 0.0
    error: str | None = None

    moments: list[dict] = []
    moment_count: int = 0
    total_seconds: float = 0.0
    master_path: str | None = None
    fcpxml_path: str | None = None
    manifest_path: str | None = None
    logs: list[str] = []


class CompilationRequest(BaseModel):
    url: str
    instructions: str = ""
    lang: str = "ja"
    start_offset: float = 0.0
    model: str = "gemini"
    project_name: str = ""
    # None means "use CLIP_FINDER_COMPILATION_THRESHOLD".
    threshold: float | None = None
    enable_chat_signals: bool = True
    enable_audio_signals: bool = True


# ─── Endpoints ───────────────────────────────────────────────────────────────


@router.post("/api/compilation/jobs")
async def create_compilation_job(req: CompilationRequest) -> dict:
    """Start a compilation run. Returns immediately with the job id."""
    url = req.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="url is required")

    model = (req.model or "gemini").strip().lower()
    if model not in VALID_MODELS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid model: {req.model!r}. Must be one of: "
                   f"{', '.join(sorted(VALID_MODELS))}.",
        )

    if req.threshold is not None and not (0.0 <= req.threshold <= 10.0):
        raise HTTPException(
            status_code=400, detail="threshold must be between 0 and 10",
        )

    job_id = uuid.uuid4().hex[:12]
    job = CompilationJob(
        id=job_id,
        url=url,
        instructions=req.instructions.strip(),
        lang=req.lang,
        start_offset=max(0.0, req.start_offset),
        model=model,
        project_name=req.project_name.strip(),
        threshold=req.threshold,
        enable_chat_signals=bool(req.enable_chat_signals),
        enable_audio_signals=bool(req.enable_audio_signals),
        created_at=time.time(),
    )
    job_state.comp_jobs[job_id] = job

    task = asyncio.create_task(_run_compilation(job_id))
    job_state.track_task(job_state.comp_tasks, job_id, task)
    return {"job_id": job_id, "status": job.status}


@router.get("/api/compilation/jobs")
async def list_compilation_jobs() -> dict:
    jobs = sorted(
        job_state.comp_jobs.values(), key=lambda j: j.created_at, reverse=True,
    )
    return {
        "jobs": [
            {
                "id": j.id,
                "url": j.url,
                "project_name": j.project_name,
                "status": j.status,
                "phase_label": j.phase_label,
                "moment_count": j.moment_count,
                "total_seconds": j.total_seconds,
                "created_at": j.created_at,
            }
            for j in jobs
        ]
    }


@router.get("/api/compilation/jobs/{job_id}")
async def get_compilation_job(job_id: str) -> dict:
    return _get_job_or_404(job_id).model_dump()


@router.get("/api/compilation/jobs/{job_id}/fcpxml")
async def download_fcpxml(job_id: str) -> FileResponse:
    """Download the timeline for File > Import in Premiere."""
    job = _get_job_or_404(job_id)
    if not job.fcpxml_path:
        raise HTTPException(status_code=404, detail="Timeline not generated yet.")
    path = Path(job.fcpxml_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Timeline file missing on disk.")
    return FileResponse(
        path, media_type="application/xml", filename=path.name,
    )


@router.get("/api/compilation/jobs/{job_id}/log")
async def stream_compilation_log(job_id: str) -> StreamingResponse:
    """SSE log stream, mirroring the Clip Finder log endpoint."""
    _get_job_or_404(job_id)

    async def _events() -> AsyncGenerator[str, None]:
        sent = 0
        while True:
            job = job_state.comp_jobs.get(job_id)
            if job is None:
                break
            while sent < len(job.logs):
                yield f"data: {job.logs[sent]}\n\n"
                sent += 1
            if job.status in ("completed", "failed"):
                yield f"data: [{job.status}]\n\n"
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(_events(), media_type="text/event-stream")


# ─── Background runner ───────────────────────────────────────────────────────


async def _run_compilation(job_id: str) -> None:
    """Drive one compilation run, recording progress on the job."""
    job = job_state.comp_jobs.get(job_id)
    if job is None:
        return

    def log(message: str) -> None:
        job.logs.append(message)
        logger.info(f"[compilation:{job_id}] {message}")

    job.status = "running"
    job.phase_label = "Analysing + downloading"

    try:
        api_keys = [k for k in getattr(config, "GEMINI_API_KEYS", []) if k]
        output_dir = job_state.COMPILATION_DIR / job_id

        result = await build_compilation(
            url=job.url,
            output_dir=output_dir,
            api_keys=api_keys,
            instructions=job.instructions,
            lang=job.lang,
            start_offset=job.start_offset,
            model=job.model,
            threshold=job.threshold,
            project_name=job.project_name,
            enable_chat=job.enable_chat_signals,
            enable_audio=job.enable_audio_signals,
            log_fn=log,
        )

        job.moments = [c.to_dict() for c in result.clips]
        job.moment_count = len(result.clips)
        job.total_seconds = round(result.total_seconds, 2)
        # Absolute, always. These paths are handed to Premiere, which runs with
        # its own working directory — a relative one resolves to nothing there
        # and surfaces as a baffling "file not found" at import time.
        job.master_path = _absolute(result.master)
        job.fcpxml_path = _absolute(result.fcpxml)
        job.manifest_path = _absolute(result.manifest)

        if result.ok:
            job.status = "completed"
            job.phase_label = (
                f"Ready — {job.moment_count} moments, "
                f"{job.total_seconds / 60:.1f} min"
            )
        else:
            job.status = "failed"
            job.error = "; ".join(result.errors) or "compilation produced no timeline"
            job.phase_label = "Failed"
            log(f"ERROR: {job.error}")

    except asyncio.CancelledError:
        job.status = "failed"
        job.phase_label = "Cancelled"
        job.error = "cancelled"
        raise
    except Exception as exc:  # noqa: BLE001 — a job failure must not kill the server
        logger.exception(f"[compilation:{job_id}] crashed")
        job.status = "failed"
        job.phase_label = "Failed"
        job.error = str(exc)
        log(f"ERROR: {exc}")


def _absolute(path: Path | str | None) -> str | None:
    """Absolute string form of a path, or None."""
    if not path:
        return None
    return str(Path(path).resolve())


def _get_job_or_404(job_id: str) -> CompilationJob:
    job = job_state.comp_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return job


__all__ = ["router", "CompilationJob", "CompilationRequest"]
