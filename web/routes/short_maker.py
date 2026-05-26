"""
web/routes/short_maker.py — Workspace 03 (Short Maker) HTTP surface.

Eight endpoints + one background task:

  - POST   /api/short-maker/upload                    upload a video
  - GET    /api/short-maker/{id}/video-info           dims + default crops
  - GET    /api/short-maker/{id}/preview-frame        single JPEG frame
  - GET    /api/short-maker/{id}/video                stream original video
  - POST   /api/short-maker/{id}/process              start the FFmpeg run
  - GET    /api/short-maker/{id}/status               poll status
  - GET    /api/short-maker/{id}/download             grab the finished short
  - POST   /api/short-maker/from-job/{src_id}         derive from existing job

The router shares state with the rest of the app via
``web.services.job_state`` — this file owns no module-level dicts of
its own.

Mounted by ``web/server.py``::

    from web.routes.short_maker import router as short_maker_router
    app.include_router(short_maker_router)
"""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from loguru import logger
from pydantic import BaseModel

from web.services import job_state


router = APIRouter()

SHORTS_OUTPUT_DIR = Path("./output/shorts")
SHORTS_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


class ShortMakerRequest(BaseModel):
    """Per-process tweak knobs.

    The two crop dicts mirror the ``CropRegion`` shape exposed by
    ``processors.short_maker``: ``{x, y, w, h}``. Both default to
    ``None`` so the renderer falls back to compute_default_*_crop().
    """

    top_crop: dict | None = None
    bottom_crop: dict | None = None
    padding: int = 0


# ─── /api/short-maker/upload ────────────────────────────────────────────────


@router.post("/api/short-maker/upload")
async def short_maker_upload(video: UploadFile = File(...)):
    """Upload a video for Short Maker processing."""
    if not (video.filename or "").lower().endswith(
        (".mp4", ".mov", ".mkv", ".avi")
    ):
        raise HTTPException(
            status_code=400,
            detail="Only video files are accepted (.mp4, .mov, .mkv, .avi)",
        )

    job_id = uuid.uuid4().hex[:12]
    upload_path = job_state.UPLOADS_DIR / f"short_{job_id}_{video.filename}"

    with upload_path.open("wb") as f:
        content = await video.read()
        f.write(content)

    job_state.short_jobs[job_id] = {
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


# ─── /api/short-maker/{id}/video-info ───────────────────────────────────────


@router.get("/api/short-maker/{job_id}/video-info")
async def short_maker_video_info(job_id: str):
    """Return video dimensions + default crops for the Short Maker UI."""
    sjob = job_state.short_jobs.get(job_id)
    if not sjob:
        raise HTTPException(status_code=404, detail="Short maker job not found")

    from processors.short_maker import (
        compute_default_bottom_crop,
        compute_default_top_crop,
        get_video_info,
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


# ─── /api/short-maker/{id}/preview-frame ────────────────────────────────────


@router.get("/api/short-maker/{job_id}/preview-frame")
async def short_maker_preview_frame(job_id: str, t: float = 0.0):
    """Extract a single preview frame from the uploaded video at ``t``."""
    sjob = job_state.short_jobs.get(job_id)
    if not sjob:
        raise HTTPException(status_code=404, detail="Short maker job not found")

    from processors.short_maker import extract_preview_frame

    frame_dir = SHORTS_OUTPUT_DIR / job_id / "frames"
    frame_dir.mkdir(parents=True, exist_ok=True)
    frame_path = frame_dir / f"frame_{t:.2f}.jpg"

    if not frame_path.exists():
        await extract_preview_frame(sjob["video_path"], frame_path, timestamp=t)

    return FileResponse(str(frame_path), media_type="image/jpeg")


# ─── /api/short-maker/{id}/video ────────────────────────────────────────────


@router.get("/api/short-maker/{job_id}/video")
async def short_maker_video(job_id: str):
    """Stream the uploaded video for preview in the Short Maker UI."""
    sjob = job_state.short_jobs.get(job_id)
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


# ─── /api/short-maker/{id}/process ──────────────────────────────────────────


@router.post("/api/short-maker/{job_id}/process")
async def short_maker_process(job_id: str, req: ShortMakerRequest):
    """Start processing the uploaded video into a YouTube Short."""
    sjob = job_state.short_jobs.get(job_id)
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
    job_state.track_task(job_state.short_tasks, job_id, task)

    return {"job_id": job_id, "status": "processing"}


# ─── /api/short-maker/{id}/status ───────────────────────────────────────────


@router.get("/api/short-maker/{job_id}/status")
async def short_maker_status(job_id: str):
    """Return the status of a short maker job (without log lines)."""
    sjob = job_state.short_jobs.get(job_id)
    if not sjob:
        raise HTTPException(status_code=404, detail="Short maker job not found")
    return {k: v for k, v in sjob.items() if k != "log_lines"}


# ─── /api/short-maker/{id}/download ─────────────────────────────────────────


@router.get("/api/short-maker/{job_id}/download")
async def short_maker_download(job_id: str):
    """Download the generated short video."""
    sjob = job_state.short_jobs.get(job_id)
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


# ─── /api/short-maker/from-job/{src} ────────────────────────────────────────


@router.post("/api/short-maker/from-job/{source_job_id}")
async def short_maker_from_job(source_job_id: str):
    """Create a Short Maker job from an existing pipeline job's video."""
    source_job = job_state.jobs.get(source_job_id)
    if not source_job:
        raise HTTPException(status_code=404, detail="Source job not found")
    if not source_job.video_path:
        raise HTTPException(status_code=400, detail="Source job has no video")

    video_file = Path(source_job.video_path)
    if not video_file.exists():
        raise HTTPException(
            status_code=404, detail="Source video file not found on disk",
        )

    job_id = uuid.uuid4().hex[:12]

    job_state.short_jobs[job_id] = {
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


# ─── Background task ─────────────────────────────────────────────────────────


async def _run_short_maker(
    job_id: str,
    top_crop_dict: dict | None,
    bottom_crop_dict: dict | None,
    padding: int,
) -> None:
    """Run the Short Maker FFmpeg pipeline in the background.

    Mutates ``job_state.short_jobs[job_id]`` in place. Never raises:
    failures land on ``status = "failed"`` and ``error = str(exc)``.
    """
    sjob = job_state.short_jobs[job_id]

    def log(msg: str) -> None:
        sjob["log_lines"].append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        logger.info("[Short {}] {}", job_id[:8], msg)

    try:
        from processors.short_maker import CropRegion, make_short

        video_path = Path(sjob["video_path"])
        output_dir = SHORTS_OUTPUT_DIR / job_id
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"short_{sjob['filename']}"

        # If output_path doesn't end with .mp4, normalise it.
        if output_path.suffix.lower() != ".mp4":
            output_path = output_path.with_suffix(".mp4")

        top_crop = (
            CropRegion.from_dict(top_crop_dict) if top_crop_dict else None
        )
        bottom_crop = (
            CropRegion.from_dict(bottom_crop_dict) if bottom_crop_dict else None
        )

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
        log(f"Short video created: {result.name}")

    except asyncio.CancelledError:
        sjob["status"] = "cancelled"
        log("Short maker cancelled")

    except Exception as exc:  # noqa: BLE001 — terminal-state guard
        sjob["status"] = "failed"
        sjob["error"] = str(exc)
        log(f"Error: {exc}")
        logger.exception("[Short {}] Failed", job_id[:8])


__all__ = ["router", "ShortMakerRequest"]
