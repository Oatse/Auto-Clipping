"""
processors/transcription.py — Phase 1: WhisperX Transcription & Alignment.

Responsibilities:
  1. Extract audio from the input video to WAV.
  2. Run WhisperX transcription with word-level timestamps.
  3. Run forced alignment (whisperx.align) for precise word timestamps.
  4. Save structured output to source_transcript.json.

Note: Diarization is disabled to avoid Pyannote/HuggingFace dependencies.
      All segments are assigned to SPEAKER_00.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from loguru import logger

import config
from models.transcript import TranscriptSegment, WordTimestamp, sanitize_timestamps
from utils.ffmpeg_utils import extract_audio, FFMPEG_BIN
from utils.file_utils import ensure_dir


class TranscriptionProcessor:
    """
    Phase 1: Ingestion & Analysis using WhisperX.

    Diarization is disabled. All segments are assigned to SPEAKER_00.
    """

    def __init__(self) -> None:
        self._model = None
        self._align_model = None
        self._align_metadata = None
        self._current_model_key: str | None = None  # Track which model is loaded


    def _load_models(self, model_key: str | None = None) -> None:
        """
        Lazy-load transcription model (deferred to avoid slow import at startup).

        Parameters
        ----------
        model_key:
            Key from config.WHISPER_MODELS dict. If None, uses default WHISPERX_MODEL.
            Supports 'whisperx' (standard) and 'faster-whisper' (local model) types.
        """
        import whisperx  # type: ignore

        # Resolve model config
        if model_key and model_key in config.WHISPER_MODELS:
            model_cfg = config.WHISPER_MODELS[model_key]
        else:
            model_key = config.WHISPERX_MODEL
            model_cfg = {
                "type": "whisperx",
                "model": config.WHISPERX_MODEL,
                "label": f"WhisperX {config.WHISPERX_MODEL}",
            }

        # If a different model is requested, unload the current one
        if self._model is not None and self._current_model_key != model_key:
            logger.info("Unloading current model '{}' to load '{}'...", self._current_model_key, model_key)
            del self._model
            self._model = None
            self._current_model_key = None
            import gc
            import torch
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()

        if self._model is None:
            model_type = model_cfg.get("type", "whisperx")
            model_path = model_cfg["model"]

            if model_type == "faster-whisper":
                # Load a local faster-whisper compatible model
                logger.info(
                    "Loading Faster-Whisper model '{}' from '{}' on {} with {}...",
                    model_cfg.get("label", model_key),
                    model_path,
                    config.WHISPERX_DEVICE,
                    config.WHISPERX_COMPUTE_TYPE,
                )
                self._model = whisperx.load_model(
                    model_path,
                    device=config.WHISPERX_DEVICE,
                    compute_type=config.WHISPERX_COMPUTE_TYPE,
                )
            else:
                # Standard whisperx model (downloads from HuggingFace)
                logger.info(
                    "Loading WhisperX model '{}' on {} with {}...",
                    model_path,
                    config.WHISPERX_DEVICE,
                    config.WHISPERX_COMPUTE_TYPE,
                )
                self._model = whisperx.load_model(
                    model_path,
                    device=config.WHISPERX_DEVICE,
                    compute_type=config.WHISPERX_COMPUTE_TYPE,
                )

            self._current_model_key = model_key

    async def transcribe(
        self,
        video_path: Path | str,
        output_dir: Path | str,
        num_speakers: int | None = None,
        speaker_detection: bool = True,
        model_key: str | None = None,
    ) -> tuple[list[TranscriptSegment], Path]:
        """
        Full Phase 1 pipeline: extract audio → transcribe → align.

        Parameters
        ----------
        video_path:
            Input video file (.mp4).
        output_dir:
            Directory to save source_transcript.json and extracted audio.
        num_speakers:
            Optional manual speaker count. When provided, caps the number of
            unique speakers assigned by the gap-based heuristic to this value.
            Only used when speaker_detection=True.
        speaker_detection:
            When False, skips gap-based speaker assignment entirely —
            all segments are assigned to SPEAKER_00. Use for single-speaker
            videos to guarantee the Pycaps rendering path (with animations).

        Returns
        -------
        tuple[list[TranscriptSegment], Path]
            Parsed segments and path to source_transcript.json.
        """
        import whisperx  # type: ignore

        video_path = Path(video_path)
        output_dir = Path(output_dir)
        ensure_dir(output_dir)

        # Step 1: Extract audio
        audio_path = output_dir / f"{video_path.stem}_audio.wav"
        logger.info(f"Step 1: Extracting audio from {video_path} to {audio_path}")
        try:
            await extract_audio(video_path, audio_path)
            logger.info("Step 1 Complete: Audio extracted.")
        except Exception as e:
            logger.error(f"Step 1 Failed: {e}")
            raise

        # Step 2: Load models
        logger.info("Step 2: Loading transcription models...")
        try:
            self._load_models(model_key=model_key)
            logger.info("Step 2 Complete: Models loaded.")
        except Exception as e:
            logger.error(f"Step 2 Failed: {e}")
            raise

        # Step 2.5: Ensure FFmpeg is in PATH for WhisperX
        ffmpeg_dir = str(Path(FFMPEG_BIN).parent)
        if ffmpeg_dir not in os.environ["PATH"]:
            logger.info(f"Adding FFmpeg directory to PATH: {ffmpeg_dir}")
            os.environ["PATH"] += os.pathsep + ffmpeg_dir
        
        # Verify FFmpeg is callable from shell now
        import shutil
        if shutil.which("ffmpeg"):
            logger.info(f"FFmpeg found in PATH: {shutil.which('ffmpeg')}")
        else:
            logger.error("FFmpeg NOT found in PATH even after update!")

        # Step 3: Transcribe
        logger.info("Step 3: Starting transcription...")
        try:
            result = self._model.transcribe(
                str(audio_path),
                batch_size=config.WHISPERX_BATCH_SIZE,
                language=config.WHISPERX_LANGUAGE,
            )
            logger.info("Step 3 Complete: Transcription finished.")
        except RuntimeError as e:
            if "CUDA failed with error out of memory" in str(e):
                logger.error(
                    "CUDA Out of Memory Error! "
                    "Try reducing WHISPERX_BATCH_SIZE in .env (e.g., from 8 to 4 or 2) "
                    "or switching WHISPERX_MODEL to 'medium' or 'small'."
                )
            logger.error(f"Step 3 Failed: {e}")
            raise
        except Exception as e:
            logger.error(f"Step 3 Failed: {e}")
            raise

        # Step 4: Forced alignment (word-level timestamps)
        logger.info("Step 4: Running forced alignment...")
        try:
            self._align_model, self._align_metadata = whisperx.load_align_model(
                language_code=result["language"],
                device=config.WHISPERX_DEVICE,
            )
            aligned = whisperx.align(
                result["segments"],
                self._align_model,
                self._align_metadata,
                str(audio_path),
                device=config.WHISPERX_DEVICE,
                return_char_alignments=False,
            )
            logger.info("Step 4 Complete: Alignment finished.")
        except Exception as e:
            logger.error(f"Step 4 Failed: {e}")
            raise

        # Step 5: Assign speakers
        if speaker_detection:
            logger.info("Step 5: Assigning speakers via gap-based detection...")
            assigned_segs = self._assign_speakers_by_gaps(
                aligned["segments"],
                max_speakers=num_speakers if num_speakers is not None else 6,
            )
            unique = set(s.get("speaker") for s in assigned_segs)
            logger.info(
                "Step 5 Complete: {} unique speaker(s) detected (cap={}).",
                len(unique),
                num_speakers if num_speakers is not None else "auto",
            )
        else:
            logger.info("Step 5: Speaker detection disabled — assigning SPEAKER_00 to all segments.")
            for seg in aligned["segments"]:
                seg["speaker"] = "SPEAKER_00"
            assigned_segs = aligned["segments"]
        final_result = {**aligned, "segments": assigned_segs}

        # Step 6: Parse into dataclasses
        segments = self._parse_segments(final_result["segments"])

        # Step 6.5: Sanitize timestamps — fix broken word/segment end times
        # that cause subtitles to linger on screen after the speaker stops.
        segments = sanitize_timestamps(segments)

        # Step 7: Save JSON
        json_path = output_dir / "source_transcript.json"
        self._save_json(segments, json_path)

        logger.info(
            "Transcription complete: {} segments → {}",
            len(segments),
            json_path,
        )
        return segments, json_path

    @staticmethod
    def _assign_speakers_by_gaps(
        segments: list[dict[str, Any]],
        gap_threshold: float = 0.6,
        max_speakers: int = 6,
    ) -> list[dict[str, Any]]:
        """
        Assign speaker IDs using silence-gap heuristics on WhisperX aligned segments.

        Logic:
        - Gap >= gap_threshold seconds between consecutive segments → speaker change.
        - Tracks a simple state: if the "current" speaker just spoke, a long pause
          likely means someone else starts talking.
        - Consecutive short-gap segments stay with the same speaker.

        This requires no Pyannote, HuggingFace token, or external model — only
        the word-aligned segment timing from WhisperX is used.

        Parameters
        ----------
        segments:
            Raw aligned segments from whisperx.align().
        gap_threshold:
            Minimum silence gap (seconds) to trigger a speaker change. Default 0.6s.
        max_speakers:
            Cap on number of unique speakers (cycles via modulo). Default 6.

        Returns
        -------
        list[dict]
            Same segments list with "speaker" key set on each entry.
        """
        if not segments:
            return segments

        current_speaker: int = 0
        last_end: float = segments[0].get("end", 0.0)

        for i, seg in enumerate(segments):
            seg_start: float = seg.get("start", 0.0)
            seg_end: float   = seg.get("end", seg_start)

            if i > 0:
                gap = seg_start - last_end
                if gap >= gap_threshold:
                    # Long pause → assume speaker change (toggle to next)
                    current_speaker = (current_speaker + 1) % max_speakers

            seg["speaker"] = f"SPEAKER_{current_speaker:02d}"
            last_end = seg_end

        return segments

    @staticmethod
    def _parse_segments(raw_segments: list[dict[str, Any]]) -> list[TranscriptSegment]:
        """Convert raw WhisperX output into TranscriptSegment dataclasses."""
        segments: list[TranscriptSegment] = []
        for seg in raw_segments:
            words = [
                WordTimestamp(
                    word=w.get("word", ""),
                    start=w.get("start", seg.get("start", 0.0)),
                    end=w.get("end", seg.get("end", 0.0)),
                    score=w.get("score", 0.0),
                )
                for w in seg.get("words", [])
            ]
            segments.append(
                TranscriptSegment(
                    start=seg["start"],
                    end=seg["end"],
                    text=seg.get("text", "").strip(),
                    speaker=seg.get("speaker", "SPEAKER_00"),
                    words=words,
                )
            )
        return segments

    @staticmethod
    def _save_json(segments: list[TranscriptSegment], path: Path) -> None:
        data = [s.to_dict() for s in segments]
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    @staticmethod
    def load_from_json(json_path: Path | str) -> list[TranscriptSegment]:
        """Load a previously saved source_transcript.json."""
        with Path(json_path).open("r", encoding="utf-8") as f:
            data = json.load(f)
        return [TranscriptSegment.from_dict(d) for d in data]
