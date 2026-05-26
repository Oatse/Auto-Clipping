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
from web.routes.auto_subtitle import (
    router as auto_subtitle_router,
    register_restore_hook as _register_jobs_restore,
)
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
app.include_router(auto_subtitle_router)
_register_jobs_restore(app)
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


# ─── Upload streaming helpers ────────────────────────────────────────────────
# Implementation lives in web/services/upload_helpers.py — imported above
# as _UploadTooLargeError / _safe_upload_name / _save_upload_streaming so
# every existing call site keeps working without renaming.


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


# ─── System Info ──────────────────────────────────────────────────────────────
# Implemented in web/routes/system.py — mounted via app.include_router above.


# ─── Job CRUD ─────────────────────────────────────────────────────────────────
# Implemented in web/routes/auto_subtitle.py — mounted via app.include_router
# at the top of this module. The startup restore hook is wired in via
# register_restore_hook().


# ─── Job Creation & Execution ─────────────────────────────────────────────────
# Implemented in web/routes/auto_subtitle.py.


# ─── Endpoint: Transcript ─────────────────────────────────────────────────────
# Implemented in web/routes/auto_subtitle.py.


# ─── Endpoint: Video Stream ───────────────────────────────────────────────────
# Implemented in web/routes/auto_subtitle.py.


# ─── Endpoint: Render (lanjutkan pipeline dari Phase 2) ──────────────────────
# Implemented in web/routes/auto_subtitle.py.


# ─── Endpoint: Export After Effects Script ───────────────────────────────────
# Implemented in web/routes/auto_subtitle.py.


# ─── Run render pipeline ────────────────────────────────────────────────────
# Implemented in web/routes/auto_subtitle.py.


# ─── SSE Log Stream ───────────────────────────────────────────────────────────
# Implemented in web/routes/auto_subtitle.py.


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


# /api/jobs/from-clip is implemented in web/routes/auto_subtitle.py.


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
