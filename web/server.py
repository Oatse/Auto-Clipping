"""
web/server.py — FastAPI app shell for the Video Clip Automation System.

This module is now a wiring-only layer: it instantiates the FastAPI
app, mounts every workspace router from ``web/routes/``, wires the
startup restore hooks, builds the Jinja2 ``templates`` instance for
the page router factory, and mounts ``/static``.

Workspaces live under ``web/routes/``:
  - auto_subtitle.py  (Workspace 01) /api/jobs/*
  - clip_finder.py    (Workspace 02) /api/clip-finder/*
  - short_maker.py    (Workspace 03) /api/short-maker/*
  - all_in.py         (Workspace 04) /api/all-in/*

Cross-cutting concerns:
  - system.py         /api/system, /api/elevenlabs/quota, /api/gemini/quota
  - pages.py          HTML template responses for /, /auto-subtitle, etc.

Shared in-memory state lives in ``web/services/job_state.py``;
streaming-upload helpers in ``web/services/upload_helpers.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# Add project root to path so ``import config`` resolves before any
# router module pulls it in transitively.
sys.path.insert(0, str(Path(__file__).parent.parent))
import config  # noqa: F401 — imported for the side-effect of loading .env.

# Workspace routers + their startup hooks.
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


# ─── Workspace routers ────────────────────────────────────────────────────────
#
# Each router lives in web/routes/* and owns its own URL prefix. The
# startup restore hooks are registered alongside their router so the
# wiring stays in one place per workspace.

app.include_router(system_router)

app.include_router(auto_subtitle_router)
_register_jobs_restore(app)

app.include_router(clip_finder_router)
_register_cf_restore(app)

app.include_router(short_maker_router)

app.include_router(all_in_router)
_register_all_in_restore(app)


# ─── Templates + page router ──────────────────────────────────────────────────
#
# Jinja2Templates is instantiated here because the page router takes it
# as a constructor argument; that keeps web/routes/pages.py free of
# filesystem I/O at import time.

TEMPLATES_DIR = Path(__file__).parent / "templates"
TEMPLATES_DIR.mkdir(exist_ok=True)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

app.include_router(build_page_router(templates))


# ─── Static files (must be last) ──────────────────────────────────────────────

STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
