"""
processors/muxer.py — Phase 4: Final FFmpeg Muxing.

Re-encodes the subtitle-rendered video using NVIDIA NVENC (h264_nvenc).
Audio is copied directly from the subtitled video (original audio preserved).
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from utils.ffmpeg_utils import run_ffmpeg
from utils.file_utils import ensure_dir


class MuxerProcessor:
    """
    Phase 4: Re-encode Pycaps subtitle video using NVENC → final MP4.

    The video from Pycaps already has burned-in subtitles and original audio.
    Uses h264_nvenc (NVIDIA GPU) for fast encode, audio stream is copied.
    """

    async def mux(
        self,
        video_path: Path | str,
        output_path: Path | str,
    ) -> Path:
        """
        Re-encode the subtitled video to the final output MP4.

        Parameters
        ----------
        video_path:
            Video file from Pycaps (has burned-in subtitles, original audio).
        output_path:
            Destination for the final subtitled MP4.

        Returns
        -------
        Path
            Path to the final output file.
        """
        video_path = Path(video_path)
        output_path = Path(output_path)
        ensure_dir(output_path.parent)

        logger.info(
            "Muxing: '{}' → '{}'",
            video_path.name,
            output_path.name,
        )

        await run_ffmpeg([
            "-hwaccel", "cuda",
            "-i", str(video_path),
            "-c:v", "h264_nvenc",
            "-preset", "p4",
            "-cq", "18",
            "-c:a", "copy",
            str(output_path),
        ])

        logger.info("✓ Final output: {}", output_path)
        return output_path
