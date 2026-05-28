"""
web/routes/clip_finder.py — Workspace 02 (Clip Finder) HTTP surface.

Twelve endpoints + three background tasks driving the Clip Finder
workspace: detect highlight clips from long-form YouTube videos via
yt-dlp + Gemini.

Endpoints:
  - GET    /api/clip-finder/available-clips           list jobs with downloaded clips
  - POST   /api/jobs/from-clip                        derive an auto-subtitle Job from a clip
  - POST   /api/clip-finder/jobs                      Phase 1: transcript + AI analysis
  - GET    /api/clip-finder/jobs/{id}                 poll one Job
  - GET    /api/clip-finder/jobs/{id}/log             SSE log stream
  - GET    /api/clip-finder/clips/{job}/{idx}         download a clip
  - GET    /api/clip-finder/clips/{job}/{idx}/stream  preview a clip
  - POST   /api/clip-finder/jobs/{id}/download-clips         Phase 2: bulk download
  - POST   /api/clip-finder/jobs/{id}/download-clip/{idx}    Phase 2: per-clip download

State is shared with the rest of the app via ``web.services.job_state``
(``cf_jobs`` / ``cf_tasks``).

Mounted by ``web/server.py``::

    from web.routes.clip_finder import (
        router as clip_finder_router,
        register_restore_hook as _register_cf_restore,
    )
    app.include_router(clip_finder_router)
    _register_cf_restore(app)
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import AsyncGenerator

from fastapi import APIRouter, BackgroundTasks, FastAPI, Form, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from loguru import logger
from pydantic import BaseModel

import config

from web.services import job_state
from web.services.job_models import Job, JobStatus


router = APIRouter()


# ─── Domain models ───────────────────────────────────────────────────────────


class ClipFinderJob(BaseModel):
    """Pydantic model for a Clip Finder Job.

    Stored in ``job_state.cf_jobs`` keyed by ``id``. Persisted to disk
    as ``{CLIP_FINDER_DIR}/{id}/job_meta.json`` so a server restart can
    resurface "Resume from disk" entries without losing analysis work.
    """

    id: str
    url: str
    instructions: str
    lang: str = "en"
    start_offset: float = 0.0           # Skip first N seconds (livestreams)
    mode: str = "single-shot"           # "single-shot" | "multi-stage"
    enable_audio_signals: bool = True
    enable_chat_signals: bool = True
    # ADR-0003 Scoring Profile — re-weights ClipScore.total to match the
    # content niche (vtuber, podcast, news, gaming, asmr). Defaults to
    # ``vtuber`` which preserves the legacy weight table byte-for-byte
    # so jobs without an explicit profile produce identical rankings.
    scoring_profile: str = "vtuber"
    status: str = "queued"
    progress_pct: float = 0.0
    phase_label: str = "Queued"
    error: str | None = None
    created_at: float = 0.0
    video_title: str | None = None
    clips: list[dict] = []              # serialized Clip.to_dict()
    clip_files: list[str] = []          # file paths of cut clips
    signals_summary: dict = {}          # { audio_peak: N, chat_spike: M, ... }
    log_lines: list[str] = []
    transcript: list[dict] = []         # full YT auto-sub transcript

    class Config:
        use_enum_values = True


class ClipFinderRequest(BaseModel):
    url: str
    instructions: str
    lang: str = "en"
    start_offset: float = 0.0
    mode: str | None = None             # override config default
    enable_audio_signals: bool | None = None
    enable_chat_signals: bool | None = None
    # ADR-0003 Scoring Profile (optional, defaults to ``vtuber`` which
    # is the legacy weighting). Mirrors the All In workspace contract.
    scoring_profile: str = "vtuber"


# ─── Internal helpers ────────────────────────────────────────────────────────


def _build_clip_finder():
    """Construct a ClipFinder instance using current config values."""
    from processors.clip_finder import ClipFinder
    return ClipFinder(
        cookies_file=getattr(config, "YTDLP_COOKIES_FILE", ""),
        cookies_browser=getattr(config, "YTDLP_COOKIES_BROWSER", ""),
        gemini_model=getattr(
            config, "CLIP_FINDER_GEMINI_MODEL", "gemini-3.5-flash",
        ),
        cache_dir=getattr(config, "CLIP_FINDER_CACHE_DIR", None),
        ffmpeg_path=getattr(config, "FFMPEG_PATH", "ffmpeg"),
    )


def _persist_cf_job(job: ClipFinderJob) -> None:
    """Save Clip Finder Job metadata so it survives server restarts."""
    job_dir = job_state.CLIP_FINDER_DIR / job.id
    job_dir.mkdir(parents=True, exist_ok=True)
    meta_file = job_dir / "job_meta.json"
    try:
        with meta_file.open("w", encoding="utf-8") as f:
            json.dump(
                job.model_dump(exclude={"log_lines"}),
                f, ensure_ascii=False, indent=2,
            )
    except Exception as exc:  # noqa: BLE001 — best-effort persistence.
        logger.warning(
            "[ClipFinder {}] could not persist meta: {}", job.id[:8], exc,
        )


def _resolve_clip_path(job_id: str, clip_idx: int) -> Path:
    """Resolve clip file by index from in-memory job or fallback fs scan."""
    if clip_idx < 0:
        raise HTTPException(status_code=404, detail="Clip not found")

    job = job_state.cf_jobs.get(job_id)
    if job and clip_idx < len(job.clip_files) and job.clip_files[clip_idx]:
        return Path(job.clip_files[clip_idx])

    clips_dir = job_state.CLIP_FINDER_DIR / job_id / "clips"
    if not clips_dir.exists():
        raise HTTPException(status_code=404, detail="Job not found")

    clip_files = sorted(clips_dir.glob("*.mp4"))
    if clip_idx >= len(clip_files):
        raise HTTPException(status_code=404, detail="Clip not found")

    return clip_files[clip_idx]


# ─── Restore hook ────────────────────────────────────────────────────────────


async def _restore_clip_finder_jobs() -> None:
    """Re-hydrate Clip Finder jobs from disk on server start.

    In-flight statuses (``transcribing`` / ``analyzing`` / ``signals`` /
    ``downloading``) are downgraded to a safe terminal state because
    the asyncio Task that was driving them died with the previous
    server process. If we have ``clips`` saved, we land at
    ``analyzed`` (UI shows "Resume download"); otherwise ``failed``.
    """
    if not job_state.CLIP_FINDER_DIR.exists():
        return

    restored = 0
    for job_dir in sorted(job_state.CLIP_FINDER_DIR.iterdir(), reverse=True):
        if not job_dir.is_dir():
            continue
        meta_file = job_dir / "job_meta.json"
        if not meta_file.exists():
            continue
        if job_dir.name in job_state.cf_jobs:
            continue
        try:
            with meta_file.open("r", encoding="utf-8") as f:
                meta = json.load(f)
            valid_keys = set(ClipFinderJob.model_fields.keys())
            filtered = {k: v for k, v in meta.items() if k in valid_keys}
            cf_job = ClipFinderJob(**filtered)
            in_flight = ("downloading", "transcribing", "analyzing", "signals")
            if cf_job.status in in_flight:
                cf_job.status = "analyzed" if cf_job.clips else "failed"
                cf_job.phase_label = (
                    f"Found {len(cf_job.clips)} clip(s) — Resume from disk"
                    if cf_job.clips
                    else "Server restarted before completion"
                )
            job_state.cf_jobs[cf_job.id] = cf_job
            restored += 1
        except Exception as exc:  # noqa: BLE001 — never crash startup.
            logger.warning(
                "[ClipFinder] could not restore {}: {}", job_dir.name, exc,
            )
    if restored:
        logger.info("[ClipFinder] restored {} job(s) from disk", restored)


def register_restore_hook(app: FastAPI) -> None:
    """Wire the startup restore hook into ``app``."""
    app.add_event_handler("startup", _restore_clip_finder_jobs)


# ─── /api/clip-finder/available-clips ────────────────────────────────────────


@router.get("/api/clip-finder/available-clips")
async def list_available_clips() -> list[dict]:
    """List Clip Finder jobs that have downloaded clips, for use in auto-subtitle."""
    result: list[dict] = []
    if not job_state.CLIP_FINDER_DIR.exists():
        return result

    for job_dir in sorted(job_state.CLIP_FINDER_DIR.iterdir(), reverse=True):
        if not job_dir.is_dir():
            continue
        clips_dir = job_dir / "clips"
        if not clips_dir.exists():
            continue

        clip_files = sorted(clips_dir.glob("*.mp4"))
        if not clip_files:
            continue

        job_id = job_dir.name
        cf_job = job_state.cf_jobs.get(job_id)

        clips_list: list[dict] = []
        for i, clip_file in enumerate(clip_files):
            clip_info: dict = {
                "index": i,
                "filename": clip_file.name,
                "path": str(clip_file),
                "size": clip_file.stat().st_size,
            }
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


# ─── POST /api/clip-finder/jobs ──────────────────────────────────────────────


@router.post("/api/clip-finder/jobs")
async def create_clip_finder_job(req: ClipFinderRequest) -> dict:
    """Create a new Clip Finder Job (Phase 1: transcript + AI analysis only)."""
    if not req.url.strip():
        raise HTTPException(status_code=400, detail="URL is required")
    # Instructions are optional — empty means "find all interesting moments"

    gemini_keys = config.GEMINI_API_KEYS
    if not gemini_keys:
        raise HTTPException(
            status_code=400, detail="No GEMINI_API_KEY set in .env",
        )

    mode = req.mode or getattr(config, "CLIP_FINDER_MODE", "single-shot")
    if mode not in ("single-shot", "multi-stage"):
        raise HTTPException(status_code=400, detail=f"Invalid mode '{mode}'")

    # ADR-0003: validate scoring profile against the canonical enum so
    # we fail fast with a clear 400 instead of silently coercing to
    # VTUBER inside the orchestrator.
    from processors.clip_finder.scoring_profiles import ScoringProfile
    try:
        scoring_profile = ScoringProfile(req.scoring_profile.lower())
    except (ValueError, AttributeError):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid scoring_profile: {req.scoring_profile!r}. "
                   "Must be one of: vtuber, podcast, news, gaming, asmr.",
        )

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
        scoring_profile=scoring_profile.value,
        created_at=time.time(),
    )
    job_state.cf_jobs[job_id] = job
    _persist_cf_job(job)

    task = asyncio.create_task(_run_clip_finder_phase1(job_id, gemini_keys))
    job_state.track_task(job_state.cf_tasks, job_id, task)

    return job.model_dump(exclude={"log_lines", "transcript"})


# ─── GET /api/clip-finder/jobs/{id} + log stream ─────────────────────────────


@router.get("/api/clip-finder/jobs/{job_id}")
async def get_clip_finder_job(job_id: str) -> dict:
    job = job_state.cf_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Clip finder job not found")
    return job.model_dump(exclude={"log_lines", "transcript"})


@router.get("/api/clip-finder/jobs/{job_id}/log")
async def stream_clip_finder_log(job_id: str):
    """SSE log stream for a Clip Finder Job."""
    job = job_state.cf_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Clip finder job not found")

    async def event_generator() -> AsyncGenerator[str, None]:
        sent = 0
        while True:
            j = job_state.cf_jobs.get(job_id)
            if not j:
                break
            for line in j.log_lines[sent:]:
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


# ─── Clip download / stream ──────────────────────────────────────────────────


@router.get("/api/clip-finder/clips/{job_id}/{clip_idx}")
async def download_clip(job_id: str, clip_idx: int):
    """Download a specific clip."""
    clip_path = _resolve_clip_path(job_id, clip_idx)
    if not clip_path.exists():
        raise HTTPException(status_code=404, detail="Clip file missing")
    return FileResponse(
        path=str(clip_path), filename=clip_path.name, media_type="video/mp4",
    )


@router.get("/api/clip-finder/clips/{job_id}/{clip_idx}/stream")
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


# ─── Phase 2 download triggers ───────────────────────────────────────────────


@router.post("/api/clip-finder/jobs/{job_id}/download-clips")
async def start_clip_download(job_id: str) -> dict:
    """Phase 2: Download every analyzed clip from YouTube."""
    job = job_state.cf_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Clip finder job not found")
    if job.status not in ("analyzed", "completed"):
        raise HTTPException(
            status_code=400,
            detail=f"Job is not ready for download (status: {job.status})",
        )
    if not job.clips:
        raise HTTPException(status_code=400, detail="No clips to download")

    job.status = "downloading"
    job.phase_label = f"Downloading {len(job.clips)} clips..."
    job.progress_pct = 70.0
    job.clip_files = []

    task = asyncio.create_task(_run_clip_download(job_id))
    job_state.track_task(job_state.cf_tasks, job_id, task)

    return job.model_dump(exclude={"log_lines", "transcript"})


@router.post("/api/clip-finder/jobs/{job_id}/download-clip/{clip_idx}")
async def start_single_clip_download(job_id: str, clip_idx: int) -> dict:
    """Phase 2: Download a single clip by index."""
    job = job_state.cf_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Clip finder job not found")
    if job.status not in ("analyzed", "completed"):
        raise HTTPException(
            status_code=400,
            detail=f"Job is not ready for download (status: {job.status})",
        )
    if clip_idx < 0 or clip_idx >= len(job.clips):
        raise HTTPException(status_code=404, detail="Clip index out of range")

    while len(job.clip_files) < len(job.clips):
        job.clip_files.append("")

    if job.clip_files[clip_idx]:
        return {"status": "already_downloaded", "clip_idx": clip_idx}

    asyncio.create_task(_run_single_clip_download(job_id, clip_idx))
    return {"status": "downloading", "clip_idx": clip_idx}


# ─── Background tasks ───────────────────────────────────────────────────────


async def _run_clip_finder_phase1(job_id: str, gemini_keys: list[str]) -> None:
    """Phase 1: transcript + multimodal signals + AI analysis.

    Mutates ``job_state.cf_jobs[job_id]`` in place. Never raises:
    failures land on ``status = "failed"`` and ``error = str(exc)``.
    """
    job = job_state.cf_jobs[job_id]
    job_dir = job_state.CLIP_FINDER_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    def log(msg: str) -> None:
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
                "Tried auto-generated and manual subtitles in multiple "
                "languages. The video may not have any captions available."
            )
            _persist_cf_job(job)
            return

        log(f"Transcript extracted: {len(transcript)} segments")

        # ── Apply start offset (livestream waiting time) ──
        if job.start_offset > 0:
            original_count = len(transcript)
            transcript = cf.filter_transcript_by_offset(
                transcript, job.start_offset,
            )
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
        kinds = Counter(s.kind.value for s in signals)
        job.signals_summary = dict(kinds)

        # ── Step 3: AI clip detection ──
        job.status = "analyzing"
        job.phase_label = f"AI analyzing ({job.mode})..."
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
            scoring_profile=job.scoring_profile,
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
        job.phase_label = (
            f"Found {len(scored_clips)} clip(s) — Ready to download"
        )
        job.progress_pct = 100.0
        log(
            f"Analysis complete! Found {len(scored_clips)} clip(s). "
            f"Top score: "
            f"{max((c.score.total_for(c.score_profile) for c in scored_clips), default=0):.2f}/10"
        )
        log("Click 'Download Clips' to fetch the video sections.")
        _persist_cf_job(job)

    except Exception as exc:  # noqa: BLE001 — terminal-state guard.
        job.status = "failed"
        job.phase_label = "Failed"
        job.error = str(exc)
        log(f"Error: {exc}")
        logger.exception("[ClipFinder {}] Phase 1 failed", job_id[:8])
        _persist_cf_job(job)


async def _run_clip_download(job_id: str) -> None:
    """Phase 2: Download every clip section using yt-dlp ``--download-sections``."""
    job = job_state.cf_jobs[job_id]
    job_dir = job_state.CLIP_FINDER_DIR / job_id

    def log(msg: str) -> None:
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

    except Exception as exc:  # noqa: BLE001 — terminal-state guard.
        job.status = "failed"
        job.phase_label = "Download failed"
        job.error = str(exc)
        log(f"Error: {exc}")
        logger.exception("[ClipFinder {}] Download failed", job_id[:8])
        _persist_cf_job(job)


async def _run_single_clip_download(job_id: str, clip_idx: int) -> None:
    """Phase 2 (single clip): Download a single clip section by index."""
    job = job_state.cf_jobs[job_id]
    job_dir = job_state.CLIP_FINDER_DIR / job_id

    def log(msg: str) -> None:
        job.log_lines.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        logger.info("[ClipFinder {}] {}", job_id[:8], msg)

    try:
        cf = _build_clip_finder()
        clip = job.clips[clip_idx]

        log(
            f"Downloading clip {clip_idx + 1}/{len(job.clips)}: "
            f"\"{clip.get('title', '')}\"..."
        )

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
                job.phase_label = (
                    f"Completed — {len(job.clip_files)} clips ready"
                )
                job.progress_pct = 100.0
        else:
            log(f"Failed to download clip {clip_idx + 1}.")

        _persist_cf_job(job)

    except Exception as exc:  # noqa: BLE001 — terminal-state guard.
        log(f"Error downloading clip {clip_idx + 1}: {exc}")
        logger.exception(
            "[ClipFinder {}] Single clip download failed", job_id[:8],
        )
        _persist_cf_job(job)


__all__ = [
    "router",
    "register_restore_hook",
    "ClipFinderJob",
    "ClipFinderRequest",
]
