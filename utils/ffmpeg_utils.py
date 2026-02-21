"""
utils/ffmpeg_utils.py — Async FFmpeg/FFprobe helper functions.

All subprocess calls are non-blocking (asyncio.create_subprocess_exec).
Errors raise FFmpegError with the captured stderr for easy debugging.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Sequence

from loguru import logger

import config


class FFmpegError(RuntimeError):
    """Raised when an FFmpeg or FFprobe subprocess exits with a non-zero code."""
    pass


def _resolve_binary(env_path: str, fallback: str) -> str:
    """Return env_path if set, else verify fallback is on PATH."""
    if env_path and env_path.strip():
        return env_path.strip()
    resolved = shutil.which(fallback)
    if resolved is None:
        raise EnvironmentError(
            f"'{fallback}' not found on PATH and no override set in .env. "
            "Install FFmpeg and ensure it is on your system PATH."
        )
    return fallback


FFMPEG_BIN: str = _resolve_binary(config.FFMPEG_PATH, "ffmpeg")
FFPROBE_BIN: str = _resolve_binary(config.FFPROBE_PATH, "ffprobe")


async def run_ffmpeg(args: Sequence[str], *, log_cmd: bool = True) -> None:
    """
    Run an FFmpeg command asynchronously.

    Parameters
    ----------
    args:
        Command-line arguments *after* the 'ffmpeg' binary, e.g.
        ["-i", "input.wav", "-af", "atempo=1.1", "output.wav"]
    log_cmd:
        Whether to log the full command at DEBUG level.

    Raises
    ------
    FFmpegError
        If the process exits with a non-zero return code.
    """
    cmd = [FFMPEG_BIN, "-y", *args]  # -y: overwrite output without prompting
    if log_cmd:
        logger.debug("FFmpeg cmd: {}", " ".join(str(a) for a in cmd))

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()

    if proc.returncode != 0:
        err_msg = stderr.decode(errors="replace")
        logger.error("FFmpeg failed (exit {}): {}", proc.returncode, err_msg)
        raise FFmpegError(
            f"FFmpeg exited with code {proc.returncode}.\n"
            f"Command: {' '.join(str(a) for a in cmd)}\n"
            f"Stderr:\n{err_msg}"
        )


async def get_audio_duration(audio_path: Path | str) -> float:
    """
    Use ffprobe to get the duration of an audio file in seconds.

    Parameters
    ----------
    audio_path:
        Path to the audio file.

    Returns
    -------
    float
        Duration in seconds.

    Raises
    ------
    FFmpegError
        If ffprobe fails or the duration cannot be parsed.
    """
    audio_path = Path(audio_path)
    if not audio_path.exists():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    cmd = [
        FFPROBE_BIN,
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        str(audio_path),
    ]
    logger.debug("FFprobe cmd: {}", " ".join(cmd))

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        err_msg = stderr.decode(errors="replace")
        raise FFmpegError(
            f"ffprobe failed (exit {proc.returncode}) on '{audio_path}'.\n"
            f"Stderr:\n{err_msg}"
        )

    try:
        probe_data = json.loads(stdout.decode())
        duration = float(probe_data["format"]["duration"])
        logger.debug("Duration of '{}': {:.4f}s", audio_path.name, duration)
        return duration
    except (KeyError, ValueError, json.JSONDecodeError) as exc:
        raise FFmpegError(
            f"Could not parse duration from ffprobe output for '{audio_path}': {exc}"
        ) from exc


async def has_audio_stream(video_path: Path | str) -> bool:
    """
    Use ffprobe to check whether a file contains at least one audio stream.

    Parameters
    ----------
    video_path:
        Path to the media file to inspect.

    Returns
    -------
    bool
        True if at least one audio stream is present, False otherwise.
    """
    cmd = [
        FFPROBE_BIN,
        "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        "-select_streams", "a",
        str(video_path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    try:
        data = json.loads(stdout.decode())
        return bool(data.get("streams"))
    except (json.JSONDecodeError, ValueError):
        return False


async def extract_audio(
    video_path: Path | str,
    output_wav: Path | str,
    sample_rate: int = config.AUDIO_SAMPLE_RATE,
    channels: int = config.AUDIO_CHANNELS,
) -> Path:
    """
    Extract audio track from a video file to a WAV file.

    Parameters
    ----------
    video_path:
        Source video file (e.g. .mp4).
    output_wav:
        Destination WAV file path.
    sample_rate:
        Target sample rate in Hz.
    channels:
        Number of audio channels (1=mono, 2=stereo).

    Returns
    -------
    Path
        Path to the created WAV file.

    Raises
    ------
    FFmpegError
        If the file has no audio stream (e.g. a video-only YouTube download
        such as format .f399) or if FFmpeg fails for any other reason.
    """
    video_path = Path(video_path)
    output_wav = Path(output_wav)
    output_wav.parent.mkdir(parents=True, exist_ok=True)

    # Guard: check for audio stream before attempting extraction
    if not await has_audio_stream(video_path):
        raise FFmpegError(
            f"No audio stream found in '{video_path.name}'.\n"
            "This file appears to be a video-only stream (e.g. a YouTube format "
            "like .f399 AV1). Please re-download the video with merged audio:\n"
            "  yt-dlp -f \"bestvideo+bestaudio/best\" --merge-output-format mp4 <URL>"
        )

    await run_ffmpeg([
        "-i", str(video_path),
        "-vn",                          # No video
        "-acodec", "pcm_s16le",         # 16-bit PCM WAV
        "-ar", str(sample_rate),
        "-ac", str(channels),
        str(output_wav),
    ])
    logger.info("Extracted audio: {} → {}", video_path.name, output_wav.name)
    return output_wav
