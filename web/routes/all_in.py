"""
web/routes/all_in.py — Workspace 04 (All In) HTTP surface.

Eleven endpoints + the on-disk restore hook for the one-shot
orchestrator that chains Clip Finder + Auto-Subtitle + Short Maker
into a single Job per ADR-0002.

  - GET    /api/all-in/presets                       caption preset names
  - POST   /api/all-in/jobs                          create + start a Job
  - GET    /api/all-in/jobs                          list all Jobs
  - GET    /api/all-in/jobs/{id}                     poll one Job
  - GET    /api/all-in/jobs/{id}/log                 SSE log stream
  - GET    /api/all-in/jobs/{id}/clips/{n}/stream    play a finished Clip
  - GET    /api/all-in/jobs/{id}/clips/{n}/download  download a finished Clip
  - GET    /api/all-in/jobs/{id}/clips/{n}/sidecar   Clip Sidecar metadata
  - POST   /api/all-in/jobs/{id}/clips/{n}/retry     retry one failed Clip
  - DELETE /api/all-in/jobs/{id}                     delete Job + on-disk files

State is shared with the rest of the app via ``web.services.job_state``
(``all_in_jobs`` / ``all_in_tasks``).

Mounted by ``web/server.py``::

    from web.routes.all_in import router as all_in_router
    app.include_router(all_in_router)

The startup restore hook is exposed as ``register_restore_hook(app)`` so
``web/server.py`` can wire it into the FastAPI lifespan without this
module owning the app instance.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import time
import uuid
from pathlib import Path
from typing import AsyncGenerator

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from loguru import logger
from pydantic import BaseModel

import config

from web.services import job_state
from web.services.all_in.models import (
    AllInJob,
    AllInJobStatus,
    AspectRatio,
    CaptionPreset,
    CutStrategyChoice,
    DetectionMode,
    ScoringProfileChoice,
)
from web.services.all_in.runner import (
    retry_clip as _retry_clip_impl,
    run_all_in_job as _run_all_in_job_impl,
)
from web.services.all_in.presets import list_preset_names


router = APIRouter()


# ─── Persistence helpers ─────────────────────────────────────────────────────


def _persist_all_in_job(job: AllInJob) -> None:
    """Write per-Job ``job_meta.json`` so a server restart can rehydrate.

    Best-effort: failures are logged but never raise — losing one
    snapshot only costs the user a re-fetch of finished clips, never
    the clips themselves which live next to the meta file.
    """
    job_dir = job_state.ALL_IN_DIR / job.id
    try:
        job_dir.mkdir(parents=True, exist_ok=True)
        with (job_dir / "job_meta.json").open("w", encoding="utf-8") as f:
            json.dump(
                job.model_dump(exclude={"log_lines", "transcript"}),
                f, ensure_ascii=False, indent=2,
            )
    except Exception as exc:  # noqa: BLE001 — best-effort persistence.
        logger.warning("[AllIn {}] persist failed: {}", job.id[:8], exc)


async def _restore_all_in_jobs() -> None:
    """Re-hydrate All In Jobs from disk on server start.

    In-flight statuses (``QUEUED`` / ``DOWNLOADING`` / ``ANALYZING`` /
    ``RENDERING``) are downgraded to ``FAILED`` because the asyncio
    Task that was driving them died with the previous server process.
    """
    if not job_state.ALL_IN_DIR.exists():
        return
    restored = 0
    for job_dir in sorted(job_state.ALL_IN_DIR.iterdir(), reverse=True):
        if not job_dir.is_dir():
            continue
        meta_file = job_dir / "job_meta.json"
        if not meta_file.exists() or job_dir.name in job_state.all_in_jobs:
            continue
        try:
            with meta_file.open("r", encoding="utf-8") as f:
                meta = json.load(f)
            valid_keys = set(AllInJob.model_fields.keys())
            filtered = {k: v for k, v in meta.items() if k in valid_keys}
            ai_job = AllInJob(**filtered)
            in_flight = {
                AllInJobStatus.QUEUED.value,
                AllInJobStatus.DOWNLOADING.value,
                AllInJobStatus.ANALYZING.value,
                AllInJobStatus.RENDERING.value,
            }
            if ai_job.status in in_flight:
                ai_job.status = AllInJobStatus.FAILED
                ai_job.phase_label = "Server restarted before completion"
            job_state.all_in_jobs[ai_job.id] = ai_job
            restored += 1
        except Exception as exc:  # noqa: BLE001 — never crash startup
            logger.warning("[AllIn] restore {}: {}", job_dir.name, exc)
    if restored:
        logger.info("[AllIn] restored {} job(s)", restored)


def register_restore_hook(app: FastAPI) -> None:
    """Wire the startup restore hook into ``app``.

    Called by ``web/server.py`` during app construction so this router
    doesn't need to own the FastAPI instance.
    """
    app.add_event_handler("startup", _restore_all_in_jobs)


# ─── Request schema ──────────────────────────────────────────────────────────


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


# ─── Internal helpers ────────────────────────────────────────────────────────


def _resolve_all_in_clip(job_id: str, clip_idx: int) -> Path:
    """Return the on-disk path of a finished Clip or raise 404."""
    job = job_state.all_in_jobs.get(job_id)
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


# ─── /api/all-in/presets ─────────────────────────────────────────────────────


@router.get("/api/all-in/presets")
async def list_all_in_presets() -> dict:
    """Return the available caption preset names for the All In form."""
    return {"presets": list_preset_names()}


# ─── POST /api/all-in/jobs ───────────────────────────────────────────────────


@router.post("/api/all-in/jobs")
async def create_all_in_job(req: AllInRequest):
    """Create and start a new All In Job."""
    if not req.url.strip():
        raise HTTPException(status_code=400, detail="URL is required")
    if not config.GEMINI_API_KEYS:
        raise HTTPException(
            status_code=400, detail="No GEMINI_API_KEY set in .env",
        )

    # Validate enums up front so we fail with 400 rather than 500.
    try:
        AspectRatio(req.aspect_ratio)
        CaptionPreset(req.caption_preset)
        DetectionMode(req.mode)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f"Invalid enum value: {exc}",
        )

    # ADR-0003 enums — validated separately for clearer error messages.
    try:
        scoring_profile = ScoringProfileChoice(req.scoring_profile)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid scoring_profile: {req.scoring_profile}",
        )
    try:
        cut_strategies = [
            CutStrategyChoice(s) for s in (req.cut_strategies or [])
        ]
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
    job_state.all_in_jobs[job_id] = job
    _persist_all_in_job(job)

    task = asyncio.create_task(
        _run_all_in_job_impl(
            job,
            output_root=job_state.ALL_IN_DIR,
            gemini_keys=config.GEMINI_API_KEYS,
        )
    )
    job_state.track_task(job_state.all_in_tasks, job_id, task)

    return job.model_dump(exclude={"log_lines", "transcript"})


# ─── List + read endpoints ───────────────────────────────────────────────────


@router.get("/api/all-in/jobs")
async def list_all_in_jobs() -> list[dict]:
    jobs = sorted(
        job_state.all_in_jobs.values(),
        key=lambda j: j.created_at,
        reverse=True,
    )
    return [j.model_dump(exclude={"log_lines", "transcript"}) for j in jobs]


@router.get("/api/all-in/jobs/{job_id}")
async def get_all_in_job(job_id: str) -> dict:
    job = job_state.all_in_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="All In job not found")
    return job.model_dump(exclude={"log_lines", "transcript"})


@router.get("/api/all-in/jobs/{job_id}/log")
async def stream_all_in_log(job_id: str):
    """SSE log stream for an All In Job."""
    if job_id not in job_state.all_in_jobs:
        raise HTTPException(status_code=404, detail="All In job not found")
    terminal = {
        AllInJobStatus.COMPLETED.value,
        AllInJobStatus.FAILED.value,
        AllInJobStatus.CANCELLED.value,
    }

    async def event_generator() -> AsyncGenerator[str, None]:
        sent = 0
        while True:
            j = job_state.all_in_jobs.get(job_id)
            if not j:
                break
            for line in j.log_lines[sent:]:
                yield f"data: {json.dumps({'line': line})}\n\n"
                sent += 1
            if j.status in terminal:
                yield (
                    f"data: {json.dumps({'done': True, 'status': j.status})}"
                    "\n\n"
                )
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ─── Per-Clip stream + download + sidecar ────────────────────────────────────


@router.get("/api/all-in/jobs/{job_id}/clips/{clip_idx}/stream")
async def stream_all_in_clip(job_id: str, clip_idx: int):
    path = _resolve_all_in_clip(job_id, clip_idx)
    return FileResponse(
        path=str(path),
        media_type="video/mp4",
        headers={"Accept-Ranges": "bytes"},
    )


@router.get("/api/all-in/jobs/{job_id}/clips/{clip_idx}/download")
async def download_all_in_clip(job_id: str, clip_idx: int):
    path = _resolve_all_in_clip(job_id, clip_idx)
    return FileResponse(
        path=str(path), filename=path.name, media_type="video/mp4",
    )


@router.get("/api/all-in/jobs/{job_id}/clips/{clip_idx}/sidecar")
async def get_all_in_clip_sidecar(job_id: str, clip_idx: int) -> dict:
    """Return the upload-ready Clip Sidecar metadata for a finished Clip.

    Reads ``{clip}.metadata.json`` written by the runner's Stage 5.
    Returns 404 if the Clip is not yet rendered or the sidecar is
    missing — Gemini outage during render leaves no sidecar but the
    Clip itself is still valid.
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


# ─── Retry one Clip / Delete Job ─────────────────────────────────────────────


@router.post("/api/all-in/jobs/{job_id}/clips/{clip_idx}/retry")
async def retry_all_in_clip(job_id: str, clip_idx: int) -> dict:
    """Retry a single failed Clip without re-downloading the source (Q10)."""
    job = job_state.all_in_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="All In job not found")
    if clip_idx < 0 or clip_idx >= len(job.clips):
        raise HTTPException(status_code=404, detail="Clip index out of range")

    task = job_state.all_in_tasks.get(job_id)
    if task and not task.done():
        raise HTTPException(status_code=409, detail="Job still running")

    async def _retry_runner() -> None:
        try:
            await _retry_clip_impl(
                job, clip_idx, output_root=job_state.ALL_IN_DIR,
            )
        except (IndexError, RuntimeError) as exc:
            job.log_lines.append(f"[retry] {exc}")

    new_task = asyncio.create_task(_retry_runner())
    job_state.track_task(job_state.all_in_tasks, job_id, new_task)

    return {"job_id": job_id, "clip_idx": clip_idx, "status": "retrying"}


@router.delete("/api/all-in/jobs/{job_id}")
async def delete_all_in_job(job_id: str) -> dict:
    """Delete an All In Job and its on-disk source + clips (Q12)."""
    job = job_state.all_in_jobs.pop(job_id, None)
    if not job:
        raise HTTPException(status_code=404, detail="All In job not found")
    task = job_state.all_in_tasks.pop(job_id, None)
    if task and not task.done():
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            # Cleanup is best-effort; cancel signal was delivered above.
            pass

    job_dir = job_state.ALL_IN_DIR / job_id
    if job_dir.exists():
        try:
            shutil.rmtree(job_dir)
        except Exception as exc:  # noqa: BLE001 — disk cleanup is best-effort.
            logger.warning("[AllIn {}] rmtree failed: {}", job_id[:8], exc)

    return {"deleted": job_id}


__all__ = [
    "router",
    "register_restore_hook",
    "AllInRequest",
]
