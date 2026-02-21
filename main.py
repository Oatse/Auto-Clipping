"""
main.py — Pipeline Orchestrator for the Video Clip Automation System.

Chains all 4 phases in sequence:
  Phase 1: Transcription (WhisperX)
  Phase 2: Translation (placeholder / LLM)
  Phase 3: Subtitle Rendering (Pycaps)
  Phase 4: Final Muxing (FFmpeg)

Usage:
    python main.py --input video.mp4 --output output/final.mp4
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from loguru import logger

import config
from processors.muxer import MuxerProcessor
from processors.subtitle_renderer import SubtitleRendererProcessor
from processors.transcription import TranscriptionProcessor
from processors.translator import TranslatorProcessor


# ─── Logging Setup ────────────────────────────────────────────────────────────

def setup_logging(output_dir: Path) -> None:
    log_path = output_dir / "pipeline.log"
    logger.remove()
    logger.add(sys.stderr, level="INFO", colorize=True,
               format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}")
    logger.add(str(log_path), level="DEBUG", rotation="10 MB",
               format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{line} | {message}")
    logger.info("Logging to: {}", log_path)


# ─── Pipeline ─────────────────────────────────────────────────────────────────

class VideoSubtitlePipeline:
    """
    Top-level orchestrator for the 4-phase auto-subtitle pipeline.
    """

    def __init__(
        self,
        input_video: Path,
        output_dir: Path,
        target_language: str = "id",
    ) -> None:
        self.input_video = input_video
        self.output_dir = output_dir
        self.target_language = target_language

        # Phase processors
        self.transcriber = TranscriptionProcessor()
        self.translator = TranslatorProcessor(target_language=target_language)
        self.subtitle_renderer = SubtitleRendererProcessor()
        self.muxer = MuxerProcessor()

    async def run(self) -> Path:
        """Execute the full pipeline end-to-end."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        setup_logging(self.output_dir)

        logger.info("=" * 60)
        logger.info("Video Clip Automation System — Starting Pipeline")
        logger.info("Input:  {}", self.input_video)
        logger.info("Output: {}", self.output_dir)
        logger.info("=" * 60)

        # ── Phase 1: Transcription ─────────────────────────────────────────
        logger.info("[Phase 1/4] Transcription & Diarization")
        segments, _ = await self.transcriber.transcribe(
            video_path=self.input_video,
            output_dir=self.output_dir / "phase1_transcription",
        )

        # ── Phase 2: Translation ───────────────────────────────────────────
        logger.info("[Phase 2/4] Translation → '{}'", self.target_language)
        translated_segments, _ = await self.translator.translate(
            segments=segments,
            output_dir=self.output_dir / "phase2_translation",
        )

        # ── Phase 3: Subtitle Rendering ────────────────────────────────────
        logger.info("[Phase 3/4] Subtitle Rendering (Pycaps)")
        pycaps_json = self.subtitle_renderer.build_pycaps_transcript(
            segments=translated_segments,
            output_dir=self.output_dir / "phase3_subtitles",
        )
        subtitled_video = self.subtitle_renderer.render(
            video_path=self.input_video,
            pycaps_transcript=pycaps_json,
            output_path=self.output_dir / "phase3_subtitles" / "subtitled.mp4",
        )

        # ── Phase 4: Final Muxing ──────────────────────────────────────────
        logger.info("[Phase 4/4] Final Muxing")
        stem = self.input_video.stem
        final_output = await self.muxer.mux(
            video_path=subtitled_video,
            output_path=self.output_dir / f"{stem}_subtitled_{self.target_language}.mp4",
        )

        logger.info("=" * 60)
        logger.info("Pipeline complete! Output: {}", final_output)
        logger.info("=" * 60)
        return final_output


# ─── CLI Entry Point ──────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Video Clip Automation System — Auto-subtitle pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py --input video.mp4
  python main.py --input video.mp4 --lang es --output ./my_output
        """,
    )
    parser.add_argument(
        "--input", "-i",
        required=True,
        type=Path,
        help="Input video file (.mp4)",
    )
    parser.add_argument(
        "--output", "-o",
        type=Path,
        default=config.OUTPUT_DIR,
        help=f"Output directory (default: {config.OUTPUT_DIR})",
    )
    parser.add_argument(
        "--lang", "-l",
        default="id",
        help="Target language BCP-47 code (default: id = Indonesian)",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()

    if not args.input.exists():
        logger.error("Input file not found: {}", args.input)
        sys.exit(1)

    pipeline = VideoSubtitlePipeline(
        input_video=args.input,
        output_dir=args.output,
        target_language=args.lang,
    )
    await pipeline.run()


if __name__ == "__main__":
    asyncio.run(main())
