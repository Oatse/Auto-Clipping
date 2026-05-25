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


# ═══════════════════════════════════════════════════════════════════════════
# All In Workspace — headless reframe API
# ═══════════════════════════════════════════════════════════════════════════
#
# These helpers are the surgical extraction described in ADR-0002.  The
# All In runner needs a function-callable reframe path: smart-static face
# crop + single-frame output, no human draws-the-box step.  The existing
# ``make_short()`` two-grid layout is unchanged and still drives the Short
# Maker workspace UI.
#
# Public API:
#   - aspect_ratio_to_dimensions(ratio)
#   - CropBox (alias of CropRegion for callers that prefer the new name)
#   - compute_smart_static_crop(video_path, ratio)
#   - reframe_to_ratio(input, output, ratio, crop)


# ``CropBox`` is the name the All In stages use; alias to CropRegion so we
# don't introduce a parallel dataclass with the same shape.
CropBox = CropRegion


# Mapping from the ``models.AspectRatio`` enum's string value to the
# (width, height) we ship.  ``None`` for ORIGINAL means "skip reframe".
_RATIO_OUTPUT: dict[str, tuple[int, int] | None] = {
    "original": None,
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
}


def aspect_ratio_to_dimensions(ratio: str) -> tuple[int, int] | None:
    """Return ``(out_w, out_h)`` for the named ratio, or ``None`` for original.

    Raises ``KeyError`` if the ratio is unknown — the caller should have
    validated via the ``AspectRatio`` enum first.
    """
    return _RATIO_OUTPUT[ratio]


# ─── Smart-static crop (face-detect once, lock crop for the whole clip) ─────

# Number of frames sampled across the clip duration for face detection.
# 20 is the sweet spot from grilling Q5 — enough samples to median over
# transient occlusions, cheap enough to run in <1s per Clip.
_FACE_SAMPLE_FRAMES = 20


async def compute_smart_static_crop(
    video_path: Path | str,
    ratio: str,
    *,
    log_fn=None,
) -> CropBox | None:
    """Compute a single locked crop rectangle for an entire Clip.

    Strategy (per design grilling Q5):
      1. Probe the source for width/height/duration.
      2. Sample ``_FACE_SAMPLE_FRAMES`` evenly-spaced frames.
      3. Run a face detector on each sample; collect bounding-box centroids.
      4. Take the median centroid → that's the crop centre.
      5. Build the largest crop rectangle that:
            - Has the target aspect ratio
            - Fits inside the source frame
            - Is centred on the median centroid (clamped to image bounds)
      6. If detection finds zero faces across all samples, return ``None``
         — the caller falls back to a centre crop.

    Returns ``None`` for the ``original`` ratio (no reframe needed) and
    when face detection cannot find any face in any sample frame.
    """
    if ratio == "original":
        return None

    out_dims = _RATIO_OUTPUT.get(ratio)
    if out_dims is None:
        return None
    out_w, out_h = out_dims
    target_ratio = out_w / out_h

    info = await get_video_info(video_path)
    if info.width <= 0 or info.height <= 0:
        return _centre_crop_for_ratio(info.width, info.height, target_ratio)

    centroids = await _sample_face_centroids(
        Path(video_path),
        info=info,
        sample_count=_FACE_SAMPLE_FRAMES,
        log_fn=log_fn,
    )

    if not centroids:
        if log_fn:
            log_fn("No faces detected in samples — falling back to centre crop")
        return _centre_crop_for_ratio(info.width, info.height, target_ratio)

    # Median centroid is robust to outliers (occluded frames, fast cuts)
    # in a way that mean is not — one bad detection at the edge of the
    # frame won't drag the crop with it.
    cx = _median([c[0] for c in centroids])
    cy = _median([c[1] for c in centroids])

    return _crop_for_ratio_centred_on(
        video_w=info.width,
        video_h=info.height,
        target_ratio=target_ratio,
        centre_x=cx,
        centre_y=cy,
    )


def _centre_crop_for_ratio(video_w: int, video_h: int, target_ratio: float) -> CropBox:
    """Largest target-ratio rectangle centred in the source frame."""
    return _crop_for_ratio_centred_on(
        video_w=video_w,
        video_h=video_h,
        target_ratio=target_ratio,
        centre_x=video_w / 2,
        centre_y=video_h / 2,
    )


def _crop_for_ratio_centred_on(
    *,
    video_w: int,
    video_h: int,
    target_ratio: float,
    centre_x: float,
    centre_y: float,
) -> CropBox:
    """Build the largest target-ratio crop centred on (centre_x, centre_y).

    Pure geometry — no I/O, no FFmpeg, easy to unit-test.  Ensures the
    output rectangle never exits the source frame; clamps the centre
    if it would.
    """
    src_ratio = video_w / video_h if video_h > 0 else target_ratio

    if src_ratio > target_ratio:
        # Source is wider than target → crop the width.
        crop_h = video_h
        crop_w = int(round(crop_h * target_ratio))
    else:
        # Source is taller than target → crop the height.
        crop_w = video_w
        crop_h = int(round(crop_w / target_ratio))

    # Even pixel dimensions keep H.264 happy (chroma subsampling).
    crop_w -= crop_w % 2
    crop_h -= crop_h % 2

    crop_x = int(round(centre_x - crop_w / 2))
    crop_y = int(round(centre_y - crop_h / 2))
    crop_x = max(0, min(crop_x, video_w - crop_w))
    crop_y = max(0, min(crop_y, video_h - crop_h))
    crop_x -= crop_x % 2
    crop_y -= crop_y % 2

    return CropBox(x=crop_x, y=crop_y, w=crop_w, h=crop_h)


def _median(values: Sequence[float]) -> float:
    """Median of a non-empty sequence; tie → average of middle pair."""
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0


async def _sample_face_centroids(
    video_path: Path,
    *,
    info: VideoInfo,
    sample_count: int,
    log_fn=None,
) -> list[tuple[float, float]]:
    """Sample N evenly-spaced frames and return face centroids in pixels.

    Tries OpenCV's bundled Haar cascade first (zero new dependency, ships
    with cv2).  If OpenCV isn't available, returns an empty list and the
    caller falls back to centre crop — never blocks the pipeline.
    """
    try:
        import cv2  # type: ignore
    except ImportError:
        if log_fn:
            log_fn("OpenCV not installed — skipping face detection")
        return []

    cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    cascade = cv2.CascadeClassifier(cascade_path)
    if cascade.empty():
        if log_fn:
            log_fn("Haar cascade failed to load — skipping face detection")
        return []

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return []

    try:
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if frame_count <= 0:
            # Fallback: derive from duration × fps so live VFR sources
            # still get sampled.
            frame_count = int(max(1.0, info.duration) * max(1.0, info.fps))

        # Evenly-spaced frame indices across the whole clip.
        step = max(1, frame_count // sample_count)
        indices = [i * step for i in range(sample_count)]

        centroids: list[tuple[float, float]] = []
        for idx in indices:
            cap.set(cv2.CAP_PROP_POS_FRAMES, float(idx))
            ok, frame = cap.read()
            if not ok or frame is None:
                continue
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            faces = cascade.detectMultiScale(
                gray,
                scaleFactor=1.2,
                minNeighbors=5,
                minSize=(48, 48),
            )
            if len(faces) == 0:
                continue
            # Pick the largest face per frame — biggest face = closest
            # speaker = the subject we want to keep on screen.
            x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
            centroids.append((x + w / 2.0, y + h / 2.0))

        if log_fn:
            log_fn(
                f"Face detection: {len(centroids)}/{len(indices)} sample frames "
                "yielded a face"
            )
        return centroids
    finally:
        cap.release()


# ─── Headless reframe-to-ratio renderer ─────────────────────────────────────

async def reframe_to_ratio(
    *,
    input_path: Path | str,
    output_path: Path | str,
    ratio: str,
    crop: CropBox | None = None,
    log_fn=None,
) -> Path:
    """Reframe ``input_path`` to the target aspect ratio.

    For ``ratio == "original"`` returns the input path unchanged
    (caller decides whether to copy or reuse).  Otherwise burns the
    crop + scale into the output via a single FFmpeg pass.

    If ``crop`` is ``None`` and the ratio needs one, computes a
    smart-static crop on the fly (or falls back to centre crop if
    detection finds no faces).
    """
    input_path = Path(input_path)
    output_path = Path(output_path)
    ensure_dir(output_path.parent)

    if ratio == "original":
        # No reframe — caller can copy or reuse the input directly.
        return input_path

    out_dims = aspect_ratio_to_dimensions(ratio)
    if out_dims is None:
        return input_path
    out_w, out_h = out_dims

    if crop is None:
        crop = await compute_smart_static_crop(input_path, ratio, log_fn=log_fn)

    if crop is None:
        info = await get_video_info(input_path)
        crop = _centre_crop_for_ratio(info.width, info.height, out_w / out_h)

    _validate_crop(crop, *(await _wh(input_path)), label="reframe")

    if log_fn:
        log_fn(
            f"Reframing to {ratio} ({out_w}×{out_h}) "
            f"using crop ({crop.x},{crop.y},{crop.w}×{crop.h})"
        )

    filter_complex = (
        f"[0:v]crop={crop.w}:{crop.h}:{crop.x}:{crop.y},"
        f"scale={out_w}:{out_h}:flags=lanczos[v]"
    )
    await run_ffmpeg([
        "-i", str(input_path),
        "-filter_complex", filter_complex,
        "-map", "[v]",
        "-map", "0:a?",
        "-c:v", "h264_nvenc",
        "-preset", "p4",
        "-cq", "20",
        "-c:a", "copy",
        "-movflags", "+faststart",
        str(output_path),
    ])
    return output_path


async def _wh(video_path: Path | str) -> tuple[int, int]:
    info = await get_video_info(video_path)
    return info.width, info.height
