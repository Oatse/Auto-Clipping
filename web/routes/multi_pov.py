"""
web/routes/multi_pov.py — Workspace 05 (Multi POV) HTTP surface.

Seven endpoints + one background task driving the Multi POV workspace:
find shared moments across 2–5 YouTube URLs from the same event.

Endpoints:
  - POST   /api/multi-pov/jobs                              Create + start job
  - GET    /api/multi-pov/jobs/{id}                         Poll job status
  - GET    /api/multi-pov/jobs/{id}/log                     SSE log stream
  - POST   /api/multi-pov/jobs/{id}/download-groups         Bulk download all groups
  - POST   /api/multi-pov/jobs/{id}/download-group/{gid}    Download one POV group
  - GET    /api/multi-pov/jobs/{id}/groups/{gid}/{src}/stream   Preview one perspective
  - GET    /api/multi-pov/jobs/{id}/groups/{gid}/{src}          Download one perspective

State is shared via ``web.services.job_state``
(``multi_pov_jobs`` / ``multi_pov_tasks``).

Mounted by ``web/server.py``:
    from web.routes.multi_pov import (
        router as multi_pov_router,
        register_restore_hook as _register_multi_pov_restore,
    )
    app.include_router(multi_pov_router)
    _register_multi_pov_restore(app)
"""

from __future__ import annotations

import asyncio
import json
import re
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

router = APIRouter()


# ─── Domain models ────────────────────────────────────────────────────────────


class POVSourceInput(BaseModel):
    """One source URL in a Multi POV job request."""
    url: str
    label: str = ""             # optional human label, e.g. "Player A"
    start_offset: float = 0.0


class SourceResultModel(BaseModel):
    """Serializable per-source result — stored on MultiPOVJob."""
    source_idx: int
    url: str
    label: str = ""
    video_title: str | None = None
    status: str = "pending"     # pending / extracting / analyzing / done / failed
    sub_phase: str = ""
    progress_pct: float = 0.0
    clips_found: int = 0
    clips: list[dict] = []
    signals_summary: dict = {}
    error: str | None = None


class MultiPOVJob(BaseModel):
    """Pydantic model for a Multi POV Job.

    Stored in ``job_state.multi_pov_jobs`` keyed by ``id``. Persisted to
    ``{MULTI_POV_DIR}/{id}/job_meta.json`` so a server restart can restore
    completed jobs.
    """
    id: str
    sources: list[POVSourceInput]   # 2–5 source URLs
    instructions: str
    lang: str = "ja"
    mode: str = "single-shot"
    scoring_profile: str = "vtuber"
    model: str = "gemini"
    enable_audio_signals: bool = True
    enable_chat_signals: bool = True

    status: str = "queued"          # queued/extracting/matching/analyzed/downloading/completed/failed
    progress_pct: float = 0.0
    phase_label: str = "Queued"
    error: str | None = None
    created_at: float = 0.0

    # Per-source results (populated as each source completes)
    source_results: list[SourceResultModel] = []

    # Final grouped results (populated after cross-matching)
    pov_groups: list[dict] = []         # POVGroup.to_dict()
    unmatched_clips: list[dict] = []    # {source_idx, clip_dict}

    log_lines: list[str] = []

    class Config:
        use_enum_values = True


class MultiPOVRequest(BaseModel):
    """Request body for POST /api/multi-pov/jobs."""
    sources: list[POVSourceInput]
    instructions: str = ""
    lang: str = "ja"
    mode: str | None = None
    scoring_profile: str = "vtuber"
    model: str | None = None
    enable_audio_signals: bool | None = None
    enable_chat_signals: bool | None = None


# ─── Internal helpers ─────────────────────────────────────────────────────────


def _build_multi_pov_finder():
    """Construct a MultiPOVFinder instance using current config values."""
    from processors.clip_finder.multi_pov import MultiPOVFinder
    return MultiPOVFinder(
        cookies_file=getattr(config, "YTDLP_COOKIES_FILE", ""),
        cookies_browser=getattr(config, "YTDLP_COOKIES_BROWSER", ""),
        gemini_model=getattr(config, "CLIP_FINDER_GEMINI_MODEL", "gemini-3.5-flash"),
        cache_dir=getattr(config, "CLIP_FINDER_CACHE_DIR", None),
        ffmpeg_path=getattr(config, "FFMPEG_PATH", "ffmpeg"),
    )


def _persist_multi_pov_job(job: MultiPOVJob) -> None:
    """Write per-Job ``job_meta.json`` so server restarts can rehydrate."""
    job_dir = job_state.MULTI_POV_DIR / job.id
    job_dir.mkdir(parents=True, exist_ok=True)
    meta_file = job_dir / "job_meta.json"
    try:
        with meta_file.open("w", encoding="utf-8") as f:
            json.dump(
                job.model_dump(exclude={"log_lines"}),
                f, ensure_ascii=False, indent=2,
            )
    except Exception as exc:  # noqa: BLE001 — best-effort persistence
        logger.warning("[MultiPOV {}] could not persist meta: {}", job.id[:8], exc)


def _slugify(text: str, max_length: int = 40) -> str:
    """Convert a string to a URL-safe slug for folder names."""
    slug = text.lower()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = slug.strip("-")
    return slug[:max_length] or "group"


def _overall_progress(job: MultiPOVJob) -> float:
    """Compute overall progress percentage from per-source progress values.

    Source pipelines account for 70% of overall progress.
    Cross-matching accounts for the remaining 30% (set explicitly by the runner).
    """
    if not job.source_results:
        return job.progress_pct
    source_share = 70.0 / len(job.source_results)
    source_total = sum(
        r.progress_pct * (source_share / 100.0)
        for r in job.source_results
    )
    return round(min(70.0, source_total), 1)


# ─── Restore hook ─────────────────────────────────────────────────────────────


async def _restore_multi_pov_jobs() -> None:
    """Re-hydrate Multi POV jobs from disk on server start."""
    if not job_state.MULTI_POV_DIR.exists():
        return

    restored = 0
    for job_dir in sorted(job_state.MULTI_POV_DIR.iterdir(), reverse=True):
        if not job_dir.is_dir():
            continue
        meta_file = job_dir / "job_meta.json"
        if not meta_file.exists():
            continue
        if job_dir.name in job_state.multi_pov_jobs:
            continue
        try:
            with meta_file.open("r", encoding="utf-8") as f:
                meta = json.load(f)
            valid_keys = set(MultiPOVJob.model_fields.keys())
            filtered = {k: v for k, v in meta.items() if k in valid_keys}
            job = MultiPOVJob(**filtered)
            in_flight = ("extracting", "matching", "downloading")
            if job.status in in_flight:
                job.status = "analyzed" if job.pov_groups else "failed"
                job.phase_label = (
                    f"Found {len(job.pov_groups)} group(s) — Resume from disk"
                    if job.pov_groups
                    else "Server restarted before completion"
                )
            job_state.multi_pov_jobs[job.id] = job
            restored += 1
        except Exception as exc:  # noqa: BLE001 — never crash startup
            logger.warning(
                "[MultiPOV] could not restore {}: {}", job_dir.name, exc
            )
    if restored:
        logger.info("[MultiPOV] restored {} job(s) from disk", restored)


def register_restore_hook(app: FastAPI) -> None:
    """Wire the startup restore hook into ``app``."""
    from web.services.startup import register_startup

    register_startup(app, _restore_multi_pov_jobs)


# ─── POST /api/multi-pov/jobs ─────────────────────────────────────────────────


@router.post("/api/multi-pov/jobs")
async def create_multi_pov_job(req: MultiPOVRequest) -> dict:
    """Create a new Multi POV job (Phase 1: detect + match)."""
    # Validate source count
    if len(req.sources) < 2:
        raise HTTPException(
            status_code=400,
            detail="Multi POV requires at least 2 source URLs.",
        )
    max_sources = getattr(config, "MULTI_POV_MAX_SOURCES", 5)
    if len(req.sources) > max_sources:
        raise HTTPException(
            status_code=400,
            detail=f"Multi POV supports at most {max_sources} source URLs.",
        )
    for i, src in enumerate(req.sources):
        if not src.url.strip():
            raise HTTPException(
                status_code=400, detail=f"Source {i} URL is empty.",
            )

    gemini_keys = config.GEMINI_API_KEYS
    if not gemini_keys:
        raise HTTPException(
            status_code=400, detail="No GEMINI_API_KEY set in .env",
        )

    mode = req.mode or getattr(config, "CLIP_FINDER_MODE", "single-shot")
    if mode not in ("single-shot", "multi-stage"):
        raise HTTPException(status_code=400, detail=f"Invalid mode '{mode}'")

    raw_model = (req.model or getattr(config, "CLIP_FINDER_MODEL", "gemini") or "gemini")
    detection_model = raw_model.strip().lower()
    valid_models = {"gemini", "claude-opus-4.6", "kiro-opus-4.7", "kiro-sonnet-4.6", "kiro-auto", "codex-gpt-5.5", "codex-gpt-5.4"}
    if detection_model not in valid_models:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid model: {req.model!r}. Must be one of: {', '.join(sorted(valid_models))}.",
        )

    # VTuber-refocus Step 5: VTuber-only — coerce any/legacy value to VTUBER.
    from processors.clip_finder.scoring_profiles import ScoringProfile
    scoring_profile = ScoringProfile.coerce(req.scoring_profile)

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
    job = MultiPOVJob(
        id=job_id,
        sources=[
            POVSourceInput(
                url=s.url.strip(),
                label=s.label.strip(),
                start_offset=max(0.0, s.start_offset),
            )
            for s in req.sources
        ],
        instructions=req.instructions.strip(),
        lang=req.lang,
        mode=mode,
        scoring_profile=scoring_profile.value,
        model=detection_model,
        enable_audio_signals=bool(enable_audio),
        enable_chat_signals=bool(enable_chat),
        created_at=time.time(),
        source_results=[
            SourceResultModel(source_idx=i, url=s.url.strip(), label=s.label.strip())
            for i, s in enumerate(req.sources)
        ],
    )
    job_state.multi_pov_jobs[job_id] = job
    _persist_multi_pov_job(job)

    task = asyncio.create_task(_run_multi_pov_phase1(job_id, gemini_keys))
    job_state.track_task(job_state.multi_pov_tasks, job_id, task)

    return job.model_dump(exclude={"log_lines"})


# ─── GET /api/multi-pov/jobs/{id} ─────────────────────────────────────────────


@router.get("/api/multi-pov/jobs/{job_id}")
async def get_multi_pov_job(job_id: str) -> dict:
    job = job_state.multi_pov_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Multi POV job not found")
    return job.model_dump(exclude={"log_lines"})


# ─── GET /api/multi-pov/jobs/{id}/log ────────────────────────────────────────


@router.get("/api/multi-pov/jobs/{job_id}/log")
async def stream_multi_pov_log(job_id: str):
    """SSE log stream for a Multi POV Job."""
    job = job_state.multi_pov_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Multi POV job not found")

    async def event_generator() -> AsyncGenerator[str, None]:
        sent = 0
        while True:
            j = job_state.multi_pov_jobs.get(job_id)
            if not j:
                break
            for line in j.log_lines[sent:]:
                yield f"data: {json.dumps({'line': line})}\n\n"
                sent += 1
            terminal = ("analyzed", "completed", "failed")
            if j.status in terminal:
                yield f"data: {json.dumps({'done': True, 'status': j.status})}\n\n"
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ─── POST /api/multi-pov/jobs/{id}/download-groups ───────────────────────────


@router.post("/api/multi-pov/jobs/{job_id}/download-groups")
async def start_bulk_group_download(job_id: str) -> dict:
    """Phase 2: Download all POV groups."""
    job = job_state.multi_pov_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Multi POV job not found")
    if job.status not in ("analyzed", "completed"):
        raise HTTPException(
            status_code=400,
            detail=f"Job is not ready for download (status: {job.status})",
        )
    if not job.pov_groups:
        raise HTTPException(status_code=400, detail="No POV groups to download")

    job.status = "downloading"
    job.phase_label = f"Downloading {len(job.pov_groups)} group(s)..."
    job.progress_pct = 70.0

    task = asyncio.create_task(_run_bulk_group_download(job_id))
    job_state.track_task(job_state.multi_pov_tasks, job_id, task)

    return job.model_dump(exclude={"log_lines"})


# ─── POST /api/multi-pov/jobs/{id}/download-group/{gid} ──────────────────────


@router.post("/api/multi-pov/jobs/{job_id}/download-group/{group_id}")
async def start_single_group_download(job_id: str, group_id: str, wait: bool = False) -> dict:
    """Phase 2: Download one POV group by group_id.

    Parameters
    ----------
    wait : bool, optional
        If True, the endpoint awaits the download before returning so that the
        caller (sequential-download loop in the frontend) can safely proceed to
        the next group without triggering concurrent yt-dlp sessions.
        Defaults to False (fire-and-forget, original behaviour).
    """
    job = job_state.multi_pov_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Multi POV job not found")
    if job.status not in ("analyzed", "completed", "downloading"):
        raise HTTPException(
            status_code=400,
            detail=f"Job is not ready for download (status: {job.status})",
        )

    group = next((g for g in job.pov_groups if g.get("group_id") == group_id), None)
    if not group:
        raise HTTPException(status_code=404, detail=f"Group '{group_id}' not found")

    if wait:
        # Sequential mode: await the download so the caller can chain safely
        await _run_single_group_download(job_id, group_id)
    else:
        asyncio.create_task(_run_single_group_download(job_id, group_id))

    return {"status": "downloading", "group_id": group_id}


# ─── GET /api/multi-pov/jobs/{id}/groups/{gid}/{src}/stream ──────────────────


@router.get("/api/multi-pov/jobs/{job_id}/groups/{group_id}/{source_idx}/stream")
async def stream_perspective(job_id: str, group_id: str, source_idx: int):
    """Stream one downloaded perspective for preview."""
    clip_path = _resolve_perspective_path(job_id, group_id, source_idx)
    if not clip_path or not clip_path.exists():
        raise HTTPException(status_code=404, detail="Clip file not found")
    return FileResponse(
        path=str(clip_path),
        media_type="video/mp4",
        headers={"Accept-Ranges": "bytes"},
    )


# ─── GET /api/multi-pov/jobs/{id}/groups/{gid}/{src} ─────────────────────────


@router.get("/api/multi-pov/jobs/{job_id}/groups/{group_id}/{source_idx}")
async def download_perspective(job_id: str, group_id: str, source_idx: int):
    """Download one perspective MP4 file."""
    clip_path = _resolve_perspective_path(job_id, group_id, source_idx)
    if not clip_path or not clip_path.exists():
        raise HTTPException(status_code=404, detail="Clip file not found")
    return FileResponse(
        path=str(clip_path),
        filename=clip_path.name,
        media_type="video/mp4",
    )


# ─── Path resolver ────────────────────────────────────────────────────────────


def _resolve_perspective_path(
    job_id: str, group_id: str, source_idx: int
) -> Path | None:
    """Find the MP4 file for a perspective by job/group/source."""
    job = job_state.multi_pov_jobs.get(job_id)
    if not job:
        return None

    # First try the in-memory file_path field
    for g in job.pov_groups:
        if g.get("group_id") != group_id:
            continue
        for p in g.get("perspectives", []):
            if p.get("source_idx") == source_idx and p.get("file_path"):
                return Path(p["file_path"])

    # Fallback: scan disk
    group_dir = job_state.MULTI_POV_DIR / job_id / "groups" / group_id
    if not group_dir.exists():
        return None
    for f in group_dir.glob("*.mp4"):
        if f.name.startswith(f"source_{source_idx}_"):
            return f
    return None


# ─── Background task: Phase 1 ─────────────────────────────────────────────────


async def _run_multi_pov_phase1(job_id: str, gemini_keys: list[str]) -> None:
    """Phase 1: per-source extraction + LLM cross-matching.

    Mutates ``job_state.multi_pov_jobs[job_id]`` in place.
    Never raises — failures land on ``status = "failed"``.
    """
    job = job_state.multi_pov_jobs[job_id]
    job_dir = job_state.MULTI_POV_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    def log(msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        job.log_lines.append(f"[{ts}] {msg}")
        logger.info("[MultiPOV {}] {}", job_id[:8], msg)

    def on_source_progress(src_idx: int, pct: float, sub_phase: str) -> None:
        """Update the in-memory per-source SourceResultModel."""
        for r in job.source_results:
            if r.source_idx == src_idx:
                r.progress_pct = pct
                r.sub_phase = sub_phase
                break
        # Recompute overall progress
        job.progress_pct = _overall_progress(job)

    try:
        from processors.clip_finder.multi_pov import MultiPOVFinder, SourceInput

        finder = _build_multi_pov_finder()

        # Build SourceInput list
        sources = [
            SourceInput(
                url=s.url,
                label=s.label,
                start_offset=s.start_offset,
                source_idx=i,
            )
            for i, s in enumerate(job.sources)
        ]

        job.status = "extracting"
        job.phase_label = f"Analyzing {len(sources)} source(s) in parallel..."
        job.progress_pct = 5.0
        log(f"Starting Multi POV pipeline: {len(sources)} source(s), mode={job.mode}")

        pov_groups, unmatched, source_results = await finder.find_pov_groups(
            sources=sources,
            instructions=job.instructions,
            api_keys=gemini_keys,
            mode=job.mode,
            scoring_profile=job.scoring_profile,
            model=job.model,
            enable_audio_signals=job.enable_audio_signals,
            enable_chat_signals=job.enable_chat_signals,
            job_dir=job_dir,
            log_fn=log,
            progress_fn=on_source_progress,
        )

        # Sync per-source results back to job model
        job.source_results = [
            SourceResultModel(
                source_idx=r.source_idx,
                url=r.url,
                label=r.label,
                video_title=r.video_title,
                status=r.status,
                sub_phase=r.sub_phase,
                progress_pct=r.progress_pct,
                clips_found=len(r.clips),
                clips=[c.to_dict() for c in r.clips],
                signals_summary=r.signals_summary,
                error=r.error,
            )
            for r in source_results
        ]

        # Cross-matching phase
        job.status = "matching"
        job.phase_label = "Cross-matching moments across sources..."
        job.progress_pct = 75.0

        # Serialize POVGroups and unmatched
        job.pov_groups = [g.to_dict() for g in pov_groups]
        job.unmatched_clips = [
            {"source_idx": src_idx, **clip.to_dict()}
            for src_idx, clip in unmatched
        ]

        # Edge case C1: no multi-POV groups — still succeed, warn in UI
        multi_count = sum(1 for g in job.pov_groups if g.get("is_multi_pov"))
        if multi_count == 0:
            log(
                "Warning: No multi-POV moments found across sources. "
                "All clips are in 'Single-Source Moments' section."
            )

        job.status = "analyzed"
        job.phase_label = (
            f"Found {multi_count} multi-POV group(s) — Ready to download"
        )
        job.progress_pct = 100.0
        log(
            f"Analysis complete! {multi_count} multi-POV group(s), "
            f"{len(job.pov_groups) - multi_count} single-source group(s), "
            f"{len(job.unmatched_clips)} unmatched clip(s)."
        )
        _persist_multi_pov_job(job)

    except Exception as exc:  # noqa: BLE001 — terminal-state guard
        job.status = "failed"
        job.phase_label = "Failed"
        job.error = str(exc)
        log(f"Error: {exc}")
        logger.exception("[MultiPOV {}] Phase 1 failed", job_id[:8])
        _persist_multi_pov_job(job)


# ─── Background task: Bulk group download ─────────────────────────────────────


async def _run_bulk_group_download(job_id: str) -> None:
    """Download all perspectives from all POV groups."""
    job = job_state.multi_pov_jobs[job_id]
    job_dir = job_state.MULTI_POV_DIR / job_id

    def log(msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        job.log_lines.append(f"[{ts}] {msg}")
        logger.info("[MultiPOV {}] {}", job_id[:8], msg)

    try:
        from processors.clip_finder import ClipFinder

        cf = ClipFinder(
            cookies_file=getattr(config, "YTDLP_COOKIES_FILE", ""),
            cookies_browser=getattr(config, "YTDLP_COOKIES_BROWSER", ""),
            gemini_model=getattr(config, "CLIP_FINDER_GEMINI_MODEL", "gemini-3.5-flash"),
            ffmpeg_path=getattr(config, "FFMPEG_PATH", "ffmpeg"),
        )

        total_perspectives = sum(
            len(g.get("perspectives", [])) for g in job.pov_groups
        )
        log(f"Downloading {total_perspectives} perspective(s) across {len(job.pov_groups)} group(s)...")

        for g_dict in job.pov_groups:
            group_id = g_dict.get("group_id", "group")
            group_dir = job_dir / "groups" / group_id
            group_dir.mkdir(parents=True, exist_ok=True)

            for p_dict in g_dict.get("perspectives", []):
                await _download_perspective(cf, job, p_dict, group_dir, log)

        # Download unmatched clips to singles/
        if job.unmatched_clips:
            singles_dir = job_dir / "singles"
            singles_dir.mkdir(parents=True, exist_ok=True)
            for unmatched in job.unmatched_clips:
                src_idx = unmatched.get("source_idx", 0)
                src = job.sources[src_idx] if src_idx < len(job.sources) else None
                if not src:
                    continue
                label = _slugify(src.label or f"source-{src_idx}")
                try:
                    paths = await cf.download_clip_sections(
                        url=src.url,
                        clips=[unmatched],
                        output_dir=singles_dir,
                        log_fn=log,
                    )
                    if paths:
                        unmatched["file_path"] = str(paths[0])
                        log(f"Unmatched clip from source {src_idx} downloaded.")
                except Exception as exc:  # noqa: BLE001
                    log(f"Unmatched clip download failed (non-fatal): {exc}")

        job.status = "completed"
        job.phase_label = "Completed — All groups downloaded"
        job.progress_pct = 100.0
        log("All POV groups downloaded successfully.")
        _persist_multi_pov_job(job)

    except Exception as exc:  # noqa: BLE001 — terminal-state guard
        job.status = "failed"
        job.phase_label = "Download failed"
        job.error = str(exc)
        log(f"Error: {exc}")
        logger.exception("[MultiPOV {}] Bulk download failed", job_id[:8])
        _persist_multi_pov_job(job)


async def _run_single_group_download(job_id: str, group_id: str) -> None:
    """Download all perspectives for one POV group."""
    job = job_state.multi_pov_jobs[job_id]
    job_dir = job_state.MULTI_POV_DIR / job_id

    def log(msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        job.log_lines.append(f"[{ts}] {msg}")
        logger.info("[MultiPOV {}] {}", job_id[:8], msg)

    try:
        from processors.clip_finder import ClipFinder

        cf = ClipFinder(
            cookies_file=getattr(config, "YTDLP_COOKIES_FILE", ""),
            cookies_browser=getattr(config, "YTDLP_COOKIES_BROWSER", ""),
            gemini_model=getattr(config, "CLIP_FINDER_GEMINI_MODEL", "gemini-3.5-flash"),
            ffmpeg_path=getattr(config, "FFMPEG_PATH", "ffmpeg"),
        )

        g_dict = next(
            (g for g in job.pov_groups if g.get("group_id") == group_id), None
        )
        if not g_dict:
            log(f"Group '{group_id}' not found in job.")
            return

        group_dir = job_dir / "groups" / group_id
        group_dir.mkdir(parents=True, exist_ok=True)

        for p_dict in g_dict.get("perspectives", []):
            await _download_perspective(cf, job, p_dict, group_dir, log)

        log(f"Group '{group_id}' downloaded successfully.")
        _persist_multi_pov_job(job)

    except Exception as exc:  # noqa: BLE001 — non-fatal for single-group
        log(f"Group download failed: {exc}")
        logger.exception("[MultiPOV {}] Group download failed", job_id[:8])
        _persist_multi_pov_job(job)


async def _download_perspective(cf, job: MultiPOVJob, p_dict: dict, group_dir: Path, log) -> None:
    """Download one perspective (helper shared by bulk and single-group runners)."""
    src_idx = p_dict.get("source_idx", 0)
    if src_idx >= len(job.sources):
        return
    src = job.sources[src_idx]
    label = _slugify(p_dict.get("label") or src.label or f"source-{src_idx}")
    filename_stem = f"source_{src_idx}_{label}"

    try:
        clip_dict = {
            "start": p_dict.get("start", 0.0),
            "end": p_dict.get("end", 0.0),
            "title": p_dict.get("title", "clip"),
        }
        paths = await cf.download_clip_sections(
            url=src.url,
            clips=[clip_dict],
            output_dir=group_dir,
            log_fn=log,
        )
        if paths:
            # Rename to descriptive name
            target = group_dir / f"{filename_stem}.mp4"
            if not target.exists():
                paths[0].rename(target)
            p_dict["file_path"] = str(target)
            log(f"  Downloaded: {target.name}")

            # Slice auto-sub transcript
            source_result = next(
                (r for r in job.source_results if r.source_idx == src_idx), None
            )
            if source_result and source_result.clips:
                # Find the matching clip in source_results by start time
                matching_clip = next(
                    (c for c in source_result.clips
                     if abs(c.get("start", -1) - clip_dict["start"]) < 0.5),
                    None,
                )
                if matching_clip:
                    try:
                        from processors.clip_finder.orchestrator import ClipFinder as _CF
                        sliced = _CF.slice_transcript_for_clip(
                            transcript=source_result.clips,
                            clip_start=clip_dict["start"],
                            clip_end=clip_dict["end"],
                        )
                        autosub_path = group_dir / f"{filename_stem}_autosub.json"
                        with autosub_path.open("w", encoding="utf-8") as f:
                            json.dump(sliced, f, ensure_ascii=False, indent=2)
                        p_dict["autosub_path"] = str(autosub_path)
                    except Exception:  # noqa: BLE001 — non-fatal
                        pass

    except Exception as exc:  # noqa: BLE001 — isolate per perspective
        log(f"  Perspective download failed (source {src_idx}, non-fatal): {exc}")


__all__ = [
    "router",
    "register_restore_hook",
    "MultiPOVJob",
    "MultiPOVRequest",
]
