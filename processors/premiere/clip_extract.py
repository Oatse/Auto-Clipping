"""
processors/premiere/clip_extract.py — Cut moments out of the master for editing.

A timeline that references one 80-minute file at four scattered points is
painful to edit, and the reason is measurable: YouTube masters are long-GOP
H.264 with a keyframe roughly every six seconds, so at 60 fps Premiere may
decode ~360 frames to show one, while seeking inside a 2.5 GB file. Scrubbing
crawls.

Cutting each moment into its own short file fixes both halves — a small file
to seek, and (when re-encoded) a keyframe every second.

**Handles.** Each clip carries padding either side of the moment, so the
in/out points stay adjustable in Premiere. This is the same idea as Premiere's
own Consolidate and Transfer: shrink the media without giving up the ability
to re-trim. The master is kept on disk regardless, so a bigger recut is always
possible.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from models.clip import Clip

LogFn = Callable[[str], None]

# Padding either side of a moment, so in/out stay adjustable after the cut.
DEFAULT_HANDLE_SECONDS = 15.0

# One keyframe per second. The whole point of re-encoding is scrubbing, and a
# long GOP would hand back the problem we are solving.
KEYFRAME_INTERVAL_SECONDS = 1.0

# CQ/CRF for the editing intermediate. Higher is smaller. 23 stays visually
# clean while keeping the short-GOP overhead in check — these files are edited
# and then re-encoded on export, so squeezing the last few dB of quality out of
# an intermediate only costs disk.
DEFAULT_QUALITY = 23


@dataclass
class ExtractedClip:
    """One moment as its own media file."""

    clip: Clip
    path: Path
    # Where the moment starts inside this file: the padding that precedes it.
    # The timeline needs this to place in/out correctly.
    handle_start: float
    duration: float

    @property
    def moment_in(self) -> float:
        return self.handle_start

    @property
    def moment_out(self) -> float:
        return self.handle_start + self.clip.duration


def has_nvenc(ffmpeg_path: str = "ffmpeg") -> bool:
    """True when NVIDIA hardware H.264 encoding is available."""
    try:
        out = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=30, check=False,
        )
        return "h264_nvenc" in (out.stdout or "")
    except Exception:  # noqa: BLE001 — absence is the expected negative
        return False


def _encode_args(fps: float, use_nvenc: bool, quality: int = DEFAULT_QUALITY) -> list[str]:
    """Encoder settings tuned for scrubbing rather than file size.

    A one-second GOP with no B-frames costs bitrate — that is the trade being
    made deliberately, since the whole point is responsive seeking. ``quality``
    (CQ/CRF, higher = smaller) claws some of it back: 23 is visually clean for
    an editing intermediate that will be re-encoded on final export anyway.
    """
    gop = max(1, int(round(fps * KEYFRAME_INTERVAL_SECONDS)))
    if use_nvenc:
        return [
            "-c:v", "h264_nvenc",
            "-preset", "p4",
            "-rc", "vbr",
            "-cq", str(quality),
            "-g", str(gop),
            "-bf", "0",          # no B-frames: cheaper to seek
            "-c:a", "aac", "-b:a", "192k",
        ]
    return [
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", str(quality),
        "-g", str(gop),
        "-bf", "0",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
    ]


async def extract_moments(
    *,
    master: Path,
    clips: Sequence[Clip],
    output_dir: Path,
    fps: float = 30.0,
    handle_seconds: float = DEFAULT_HANDLE_SECONDS,
    reencode: bool = True,
    quality: int = DEFAULT_QUALITY,
    master_duration: float = 0.0,
    ffmpeg_path: str = "ffmpeg",
    log_fn: LogFn | None = None,
) -> list[ExtractedClip]:
    """Cut each moment (plus handles) out of ``master``.

    ``reencode=False`` stream-copies instead: far faster, but cuts land on the
    nearest keyframe, so the handles absorb the drift rather than the moment.
    Clips that fail are skipped and reported; the caller falls back to
    referencing the master for those.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    use_nvenc = reencode and has_nvenc(ffmpeg_path)
    if log_fn and reencode:
        log_fn(
            f"Cutting {len(clips)} moment(s) for smooth editing "
            f"({'GPU' if use_nvenc else 'CPU'} encode, keyframe every "
            f"{KEYFRAME_INTERVAL_SECONDS:.0f}s)"
        )

    extracted: list[ExtractedClip] = []
    for index, clip in enumerate(clips, start=1):
        start = max(0.0, clip.start - handle_seconds)
        end = clip.end + handle_seconds
        if master_duration > 0:
            end = min(end, master_duration)
        if end <= start:
            continue

        out_path = output_dir / f"moment_{index:03d}.mp4"
        args = [
            ffmpeg_path,
            "-hide_banner", "-loglevel", "error", "-y",
            # -ss before -i seeks fast; with re-encoding the cut is still
            # frame-accurate because ffmpeg decodes from the preceding
            # keyframe and starts output at the requested time.
            "-ss", f"{start:.3f}",
            "-i", str(master),
            "-t", f"{end - start:.3f}",
        ]
        args += (
            _encode_args(fps, use_nvenc, quality)
            if reencode
            else ["-c", "copy", "-avoid_negative_ts", "make_zero"]
        )
        args.append(str(out_path))

        ok = await _run(args)
        if not ok or not out_path.is_file() or out_path.stat().st_size == 0:
            if log_fn:
                log_fn(f"  moment {index}: extraction failed, will use the master")
            continue

        extracted.append(
            ExtractedClip(
                clip=clip,
                path=out_path,
                handle_start=clip.start - start,
                duration=end - start,
            )
        )
        if log_fn:
            size_mb = out_path.stat().st_size / (1024 * 1024)
            log_fn(f"  moment {index}/{len(clips)} cut ({size_mb:.0f} MB)")

    return extracted


async def _run(args: list[str]) -> bool:
    def _go() -> bool:
        try:
            done = subprocess.run(args, capture_output=True, text=True, check=False)
            return done.returncode == 0
        except Exception:  # noqa: BLE001
            return False

    return await asyncio.to_thread(_go)


__all__ = [
    "ExtractedClip",
    "extract_moments",
    "has_nvenc",
    "DEFAULT_HANDLE_SECONDS",
    "KEYFRAME_INTERVAL_SECONDS",
]
