"""
processors/muxer.py — Phase 4: Final FFmpeg Muxing.

Re-encodes the subtitle-rendered video.  Tries NVIDIA NVENC (h264_nvenc)
first for fast GPU encode and automatically falls back to CPU libx264 when
NVENC is unavailable (no NVIDIA GPU, missing driver, NVENC session limit).
Audio is copied directly from the subtitled video so the original track
is preserved bit-for-bit.
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from utils.ffmpeg_utils import FFmpegError, run_ffmpeg
from utils.file_utils import ensure_dir


class MuxerProcessor:
    """
    Phase 4: Re-encode the subtitle video to the final MP4 output.

    Encoder priority:
      1. ``h264_nvenc`` (NVIDIA GPU) — fast path.
      2. ``libx264`` (CPU)         — fallback when NVENC fails.

    Audio stream is copied directly from the input.
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
            "Muxing: '{}' -> '{}'",
            video_path.name,
            output_path.name,
        )

        for use_nvenc in (True, False):
            encoder_label = "NVENC/GPU" if use_nvenc else "libx264/CPU"
            try:
                await run_ffmpeg(self._build_args(
                    video_path, output_path, use_nvenc=use_nvenc,
                ))
                logger.info(
                    "Final output ({} encoder): {}", encoder_label, output_path,
                )
                return output_path
            except FFmpegError as exc:
                if use_nvenc:
                    logger.warning(
                        "Muxer NVENC failed, falling back to CPU encoder: {}",
                        str(exc).splitlines()[0] if str(exc) else exc,
                    )
                    continue
                # CPU fallback also failed — propagate the original error.
                raise

        # Should be unreachable: the for-loop either returns or raises.
        raise FFmpegError("Muxer exhausted all encoder fallbacks")

    @staticmethod
    def _build_args(
        video_path: Path,
        output_path: Path,
        *,
        use_nvenc: bool,
    ) -> list[str]:
        """Build the FFmpeg argv for a given encoder choice."""
        args: list[str] = []
        if use_nvenc:
            args += ["-hwaccel", "cuda"]
        args += ["-i", str(video_path)]

        if use_nvenc:
            args += ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "18"]
        else:
            # libx264 CRF 18 ≈ NVENC cq 18 in perceptual quality.
            args += ["-c:v", "libx264", "-preset", "medium", "-crf", "18"]

        # Copy audio bit-for-bit, strip embedded subtitle streams.
        args += ["-c:a", "copy", "-sn", str(output_path)]
        return args
