"""
processors/short_maker.py — YouTube Shorts Maker.

Creates a vertical (9:16 / 1080×1920) video from a landscape input using a
2-grid layout:
  - Top grid (1080×960): Center-cropped gameplay / main scene
  - Bottom grid (1080×960): Zoomed crop of VTuber / face-cam area

Uses FFmpeg complex filtergraph with NVIDIA NVENC for fast encoding.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Sequence

from loguru import logger

import config
from utils.ffmpeg_utils import run_ffmpeg, FFPROBE_BIN, FFmpegError
from utils.file_utils import ensure_dir


# ─── Data Models ──────────────────────────────────────────────────────────────

@dataclass
class CropRegion:
    """Defines a crop region in the source video (pixel coordinates)."""
    x: int
    y: int
    w: int
    h: int

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> CropRegion:
        return cls(
            x=int(d.get("x", 0)),
            y=int(d.get("y", 0)),
            w=int(d.get("w", 0)),
            h=int(d.get("h", 0)),
        )


@dataclass
class VideoInfo:
    """Basic video metadata from ffprobe."""
    width: int
    height: int
    duration: float
    fps: float


# ─── Utility Functions ────────────────────────────────────────────────────────

async def get_video_info(video_path: Path | str) -> VideoInfo:
    """
    Use ffprobe to get video dimensions, duration, and frame rate.

    Returns
    -------
    VideoInfo
        Video width, height, duration in seconds, and fps.
    """
    video_path = Path(video_path)
    if not video_path.exists():
        raise FileNotFoundError(f"Video file not found: {video_path}")

    cmd = [
        FFPROBE_BIN,
        "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        "-show_format",
        "-select_streams", "v:0",
        str(video_path),
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        err_msg = stderr.decode(errors="replace")
        raise FFmpegError(
            f"ffprobe failed (exit {proc.returncode}) on '{video_path}'.\n"
            f"Stderr:\n{err_msg}"
        )

    data = json.loads(stdout.decode())
    stream = data.get("streams", [{}])[0]
    fmt = data.get("format", {})

    width = int(stream.get("width", 1920))
    height = int(stream.get("height", 1080))
    duration = float(fmt.get("duration", stream.get("duration", 0)))

    # Parse fps from r_frame_rate (e.g., "30/1" or "30000/1001")
    fps_str = stream.get("r_frame_rate", "30/1")
    try:
        num, den = fps_str.split("/")
        fps = float(num) / float(den) if float(den) != 0 else 30.0
    except (ValueError, ZeroDivisionError):
        fps = 30.0

    return VideoInfo(width=width, height=height, duration=duration, fps=fps)


async def extract_preview_frame(
    video_path: Path | str,
    output_path: Path | str,
    timestamp: float = 0.0,
) -> Path:
    """
    Extract a single frame from a video at the given timestamp as JPEG.

    Parameters
    ----------
    video_path:
        Source video file.
    output_path:
        Destination JPEG file.
    timestamp:
        Time in seconds to extract the frame from.

    Returns
    -------
    Path
        Path to the extracted JPEG frame.
    """
    video_path = Path(video_path)
    output_path = Path(output_path)
    ensure_dir(output_path.parent)

    await run_ffmpeg([
        "-ss", str(timestamp),
        "-i", str(video_path),
        "-frames:v", "1",
        "-q:v", "2",
        str(output_path),
    ])

    return output_path


def compute_default_top_crop(video_width: int, video_height: int) -> CropRegion:
    """
    Compute default center crop for the top grid.

    Targets a 9:8 aspect ratio (1080:960) from the center of the video.
    """
    target_ratio = 9 / 8

    if video_width / video_height > target_ratio:
        # Video is wider than target — crop width
        crop_h = video_height
        crop_w = int(video_height * target_ratio)
    else:
        # Video is taller than target — crop height
        crop_w = video_width
        crop_h = int(video_width / target_ratio)

    crop_x = (video_width - crop_w) // 2
    crop_y = (video_height - crop_h) // 2

    return CropRegion(x=crop_x, y=crop_y, w=crop_w, h=crop_h)


def compute_default_bottom_crop(video_width: int, video_height: int) -> CropRegion:
    """
    Compute default crop for the bottom grid (VTuber/face cam).

    Assumes face cam is in the bottom-right corner, roughly 25-30% of the
    video dimensions. Targets 9:8 aspect ratio.
    """
    # Assume face cam is about 25% of video area, bottom-right corner
    cam_w = int(video_width * 0.28)
    cam_h = int(video_height * 0.35)

    # Ensure 9:8 aspect ratio
    target_ratio = 9 / 8
    if cam_w / cam_h > target_ratio:
        cam_w = int(cam_h * target_ratio)
    else:
        cam_h = int(cam_w / target_ratio)

    # Position: bottom-right with small margin
    margin = int(min(video_width, video_height) * 0.02)
    crop_x = video_width - cam_w - margin
    crop_y = video_height - cam_h - margin

    return CropRegion(x=crop_x, y=crop_y, w=cam_w, h=cam_h)


# ─── Short Maker Processor ───────────────────────────────────────────────────

# Output dimensions for YouTube Shorts
SHORT_WIDTH = 1080
SHORT_HEIGHT = 1920
GRID_HEIGHT = SHORT_HEIGHT // 2  # 960 per grid cell


async def make_short(
    input_video: Path | str,
    output_path: Path | str,
    top_crop: CropRegion | None = None,
    bottom_crop: CropRegion | None = None,
    *,
    padding: int = 0,
    log_fn=None,
) -> Path:
    """
    Create a YouTube Short (1080×1920) with 2-grid vertical layout.

    Parameters
    ----------
    input_video:
        Source video file (typically landscape 16:9).
    output_path:
        Destination file for the generated short.
    top_crop:
        Crop region for the top grid (gameplay/scene). If None, auto center.
    bottom_crop:
        Crop region for the bottom grid (VTuber/face cam). If None, auto
        defaults to bottom-right corner.
    padding:
        Optional pixel gap between top and bottom grids (black).
    log_fn:
        Optional callback for progress logging.

    Returns
    -------
    Path
        Path to the generated short video.
    """
    input_video = Path(input_video)
    output_path = Path(output_path)
    ensure_dir(output_path.parent)

    def _log(msg: str):
        if log_fn:
            log_fn(msg)
        logger.info(msg)

    # Get video dimensions
    _log("Analyzing input video...")
    info = await get_video_info(input_video)
    _log(f"Input: {info.width}×{info.height}, {info.duration:.1f}s, {info.fps:.1f}fps")

    # Compute default crop regions if not provided
    if top_crop is None:
        top_crop = compute_default_top_crop(info.width, info.height)
        _log(f"Auto top crop: x={top_crop.x}, y={top_crop.y}, "
             f"w={top_crop.w}, h={top_crop.h}")

    if bottom_crop is None:
        bottom_crop = compute_default_bottom_crop(info.width, info.height)
        _log(f"Auto bottom crop: x={bottom_crop.x}, y={bottom_crop.y}, "
             f"w={bottom_crop.w}, h={bottom_crop.h}")

    # Validate crop regions
    _validate_crop(top_crop, info.width, info.height, "top")
    _validate_crop(bottom_crop, info.width, info.height, "bottom")

    # Calculate grid heights accounting for padding
    if padding > 0:
        top_h = (SHORT_HEIGHT - padding) // 2
        bot_h = SHORT_HEIGHT - padding - top_h
    else:
        top_h = GRID_HEIGHT
        bot_h = GRID_HEIGHT

    # Build FFmpeg complex filtergraph
    filter_complex = (
        f"[0:v]crop={top_crop.w}:{top_crop.h}:{top_crop.x}:{top_crop.y},"
        f"scale={SHORT_WIDTH}:{top_h}:flags=lanczos[top];"
        f"[0:v]crop={bottom_crop.w}:{bottom_crop.h}:{bottom_crop.x}:{bottom_crop.y},"
        f"scale={SHORT_WIDTH}:{bot_h}:flags=lanczos[bottom];"
    )

    if padding > 0:
        # Add black padding bar between grids
        filter_complex += (
            f"color=black:{SHORT_WIDTH}x{padding}:d={info.duration}:r={info.fps}[pad];"
            f"[top][pad][bottom]vstack=inputs=3[out]"
        )
    else:
        filter_complex += "[top][bottom]vstack=inputs=2[out]"

    _log(f"Generating short: {SHORT_WIDTH}×{SHORT_HEIGHT}")
    _log("Encoding with NVENC GPU acceleration...")

    ffmpeg_args = [
        "-hwaccel", "cuda",
        "-i", str(input_video),
        "-filter_complex", filter_complex,
        "-map", "[out]",
        "-map", "0:a?",          # Map audio if exists (? = optional)
        "-c:v", "h264_nvenc",
        "-preset", "p4",
        "-cq", "18",
        "-c:a", "aac",
        "-b:a", "192k",
        str(output_path),
    ]

    await run_ffmpeg(ffmpeg_args)

    _log(f"✓ Short created: {output_path.name}")
    return output_path


def _validate_crop(crop: CropRegion, video_w: int, video_h: int, label: str):
    """Validate that a crop region fits within the video dimensions."""
    if crop.w <= 0 or crop.h <= 0:
        raise ValueError(f"{label} crop: width and height must be positive")
    if crop.x < 0 or crop.y < 0:
        raise ValueError(f"{label} crop: x and y must be non-negative")
    if crop.x + crop.w > video_w:
        raise ValueError(
            f"{label} crop exceeds video width: "
            f"x({crop.x}) + w({crop.w}) = {crop.x + crop.w} > {video_w}"
        )
    if crop.y + crop.h > video_h:
        raise ValueError(
            f"{label} crop exceeds video height: "
            f"y({crop.y}) + h({crop.h}) = {crop.y + crop.h} > {video_h}"
        )
