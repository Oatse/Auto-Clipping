"""
web/services/job_state.py — Shared in-memory state for the four workspace routers.

Holds the four Job dicts, four async-task dicts, and the four output
directories that pre-existed inside ``web/server.py`` as module
globals. Pulling them into one module lets the per-workspace routers
import their own slice without dragging the whole FastAPI app along.

Why this is *not* a JobRegistry class:

  - The previous design point used four module-level dicts. The
    behaviour we already ship — restore-on-startup, persist-on-status-
    change, task tracking — is a stable contract. A class wrapper
    would only add ceremony.
  - All four workspaces share the same lifecycle shape (queue → run →
    persist → terminal). Encoding that into a class would invite
    workspace-specific overrides that break the simple contract; the
    free-standing dicts force every workspace to spell out its own
    semantics, which is what we want.

Backward-compatible:

  - ``web/server.py`` still aliases the names it always used
    (``_jobs``, ``_cf_jobs``, ``_short_jobs``, ``_all_in_jobs`` and
    their ``*_tasks`` counterparts) so existing call sites keep
    working. Future router extractions import from this module
    directly.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any


# ─── Output directories ──────────────────────────────────────────────────────
#
# Created at import time so route handlers can write into them without
# guarding every call. The four workspaces store their job artefacts
# under a per-workspace subdirectory of ``OUTPUT_ROOT``; the uploads
# dir is shared across auto-subtitle and short-maker.

OUTPUT_ROOT: Path = Path("./output")
UPLOADS_DIR: Path = OUTPUT_ROOT / "uploads"
CLIP_FINDER_DIR: Path = OUTPUT_ROOT / "clip_finder"
ALL_IN_DIR: Path = OUTPUT_ROOT / "all_in"

for _d in (OUTPUT_ROOT, UPLOADS_DIR, CLIP_FINDER_DIR, ALL_IN_DIR):
    _d.mkdir(parents=True, exist_ok=True)


# ─── Auto-Subtitle Workspace (Workspace · 01) ────────────────────────────────
#
# Job model: ``web.services.job_models.Job``. Typed as ``Any`` here so
# this module stays import-cheap for the route modules — the auto-
# subtitle router does its own typing on the way in / out.

jobs: dict[str, Any] = {}
job_tasks: dict[str, asyncio.Task] = {}


# ─── Clip Finder Workspace (Workspace · 02) ──────────────────────────────────
#
# Job model: ``web.server.ClipFinderJob`` — currently defined inline in
# the server module. Once the clip-finder router is extracted, the
# model will move to ``web/services/clip_finder/models.py`` and this
# annotation can tighten up.

cf_jobs: dict[str, Any] = {}
cf_tasks: dict[str, asyncio.Task] = {}


# ─── Short Maker Workspace (Workspace · 03) ──────────────────────────────────
#
# Short Maker still uses a plain dict for its per-job state (legacy —
# never got a Pydantic model). Keep the dict-of-dicts shape until the
# router extraction; that's not the moment to also redesign the model.

short_jobs: dict[str, dict] = {}
short_tasks: dict[str, asyncio.Task] = {}


# ─── All In Workspace (Workspace · 04) ───────────────────────────────────────
#
# Job model: ``web.services.all_in.models.AllInJob``. Typed as ``Any``
# for the same reason as ``jobs`` above — see comment on the
# auto-subtitle slot.

all_in_jobs: dict[str, Any] = {}
all_in_tasks: dict[str, asyncio.Task] = {}


# ─── Task lifecycle helper ───────────────────────────────────────────────────


def track_task(
    task_dict: dict[str, asyncio.Task],
    job_id: str,
    task: asyncio.Task,
) -> None:
    """Register ``task`` against ``job_id`` and auto-clear on completion.

    The four workspaces all need the same teardown: drop the task from
    the tracker once it finishes (success, failure, cancellation) so
    the dict doesn't grow unbounded across many jobs. This helper
    encapsulates the ``add_done_callback`` dance once instead of
    duplicating it across every router.

    Idempotent: calling twice with the same ``job_id`` overwrites the
    previous task — the caller is responsible for cancelling the
    previous one if that semantic isn't what they want.
    """
    task_dict[job_id] = task

    def _cleanup(_t: asyncio.Task) -> None:
        if task_dict.get(job_id) is _t:
            task_dict.pop(job_id, None)

    task.add_done_callback(_cleanup)


__all__ = [
    "OUTPUT_ROOT",
    "UPLOADS_DIR",
    "CLIP_FINDER_DIR",
    "ALL_IN_DIR",
    "jobs",
    "job_tasks",
    "cf_jobs",
    "cf_tasks",
    "short_jobs",
    "short_tasks",
    "all_in_jobs",
    "all_in_tasks",
    "track_task",
]
