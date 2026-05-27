"""
processors.stt.elevenlabs — ElevenLabs Scribe Speech-to-Text engine.

Uses the ElevenLabs Scribe API to transcribe audio with word-level
timestamps.  Produces one segment per speaker turn (or sentence
boundary on long pauses).  Subtitle-level grouping is deferred to
Gemini during the translation phase.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

import config
from models.transcript import TranscriptSegment, WordTimestamp, sanitize_timestamps
from utils.ffmpeg_utils import extract_audio
from utils.file_utils import ensure_dir

from .base import SttEngine


API_URL = "https://api.elevenlabs.io/v1/speech-to-text"


class ElevenLabsSttEngine(SttEngine):
    """Transcribe audio via ElevenLabs Speech-to-Text API."""

    def __init__(self, api_keys: list[str] | None = None) -> None:
        self.api_keys = api_keys or config.ELEVENLABS_API_KEYS
        if not self.api_keys:
            raise ValueError("ELEVENLABS_API_KEY is not set")
        # Primary key for logging/display
        self.api_key = self.api_keys[0]

    async def transcribe(
        self,
        video_path: Path | str,
        output_dir: Path | str,
        *,
        speaker_detection: bool = True,
        num_speakers: int | None = None,
    ) -> tuple[list[TranscriptSegment], Path]:
        """
        Full ElevenLabs STT pipeline: extract audio -> API call -> parse segments.

        Returns
        -------
        tuple[list[TranscriptSegment], Path]
            Parsed segments and path to source_transcript.json.
        """
        video_path = Path(video_path)
        output_dir = Path(output_dir)
        ensure_dir(output_dir)

        # Step 1: Extract audio
        audio_path = output_dir / f"{video_path.stem}_audio.wav"
        logger.info("ElevenLabs STT Step 1: Extracting audio from {}", video_path)
        await extract_audio(video_path, audio_path)
        logger.info("Audio extracted: {}", audio_path)

        # Step 2: Call ElevenLabs API
        logger.info("ElevenLabs STT Step 2: Sending audio to ElevenLabs API...")
        raw_result = await self._call_api(
            audio_path, diarize=speaker_detection, num_speakers=num_speakers
        )
        logger.info("ElevenLabs API response received")

        # Save raw response for debugging
        raw_path = output_dir / "elevenlabs_raw.json"
        with raw_path.open("w", encoding="utf-8") as f:
            json.dump(raw_result, f, indent=2, ensure_ascii=False)

        # Step 3: Parse into segments with word timestamps
        logger.info("ElevenLabs STT Step 3: Parsing response into segments...")
        segments = self._parse_response(raw_result, speaker_detection)
        logger.info("Parsed {} segments from ElevenLabs response", len(segments))

        # Hard fail when ElevenLabs returns nothing usable. The previous
        # behaviour silently wrote an empty source_transcript.json and the
        # editor opened against a blank transcript pane (or the recent-jobs
        # path showed "No project loaded"), with no signal as to why. Raise
        # so the route layer marks the job FAILED with a real error message.
        if not segments:
            raw_words_count = len(raw_result.get("words", []))
            raw_text_len = len(raw_result.get("text", "") or "")
            lang_prob = raw_result.get("language_probability")
            lang_code = raw_result.get("language_code")
            duration = raw_result.get("audio_duration_secs")
            raise RuntimeError(
                "ElevenLabs returned no usable transcript "
                f"(language_code={lang_code!r}, "
                f"language_probability={lang_prob}, "
                f"audio_duration_secs={duration}, "
                f"raw_words={raw_words_count}, raw_text_len={raw_text_len}). "
                "Check that the audio actually contains speech and that the "
                "language is supported by Scribe."
            )

        # Log actual vs requested speaker count for transparency
        actual_speakers = set(seg.speaker for seg in segments)
        logger.info(
            "ElevenLabs detected {} unique speaker(s): {}",
            len(actual_speakers),
            sorted(actual_speakers),
        )
        if num_speakers is not None and len(actual_speakers) < num_speakers:
            logger.warning(
                "Requested max {} speakers but ElevenLabs only detected {}. "
                "This is normal — num_speakers sets the maximum, not exact count.",
                num_speakers,
                len(actual_speakers),
            )

        # Step 3.4: Persist the raw, *pre-sanitization* segment data so the
        # preview UI can display exactly what ElevenLabs reported.  Without
        # this snapshot the "ElevenLabs Original" toggle would only show the
        # post-sanitized version (timing mutated by sanitize_timestamps).
        raw_words_path = output_dir / "elevenlabs_words_raw.json"
        self._save_json(segments, raw_words_path)
        logger.info("Raw (pre-sanitization) ElevenLabs words saved: {}", raw_words_path.name)

        # Step 3.5: Sanitize timestamps via the canonical timing sanitizer.
        segments = sanitize_timestamps(segments)

        # Step 4: Save transcript
        json_path = output_dir / "source_transcript.json"
        self._save_json(segments, json_path)
        logger.info("Transcript saved: {}", json_path)

        return segments, json_path

    async def _call_api(
        self,
        audio_path: Path,
        diarize: bool = True,
        num_speakers: int | None = None,
    ) -> dict:
        """Call the ElevenLabs Speech-to-Text API, rotating keys on error."""
        last_error: str = "no keys configured"

        for key_idx, api_key in enumerate(self.api_keys):
            key_label = f"Key #{key_idx + 1}"
            headers = {"xi-api-key": api_key}

            try:
                async with httpx.AsyncClient(timeout=600.0) as client:
                    with audio_path.open("rb") as f:
                        files = {"file": (audio_path.name, f, "audio/wav")}
                        data = {
                            "model_id": "scribe_v1",
                            "timestamps_granularity": "word",
                            "diarize": str(diarize).lower(),
                            "tag_audio_events": "false",
                        }
                        if num_speakers is not None and diarize:
                            data["num_speakers"] = str(num_speakers)
                            logger.info(
                                "ElevenLabs: num_speakers={} specified",
                                num_speakers,
                            )
                        response = await client.post(
                            API_URL,
                            headers=headers,
                            files=files,
                            data=data,
                        )

                if response.status_code == 200:
                    if key_idx > 0:
                        logger.info(
                            "ElevenLabs STT: succeeded with fallback {}",
                            key_label,
                        )
                    return response.json()

                error_detail = response.text
                logger.warning(
                    "ElevenLabs {} failed (HTTP {}): {} — trying next key",
                    key_label,
                    response.status_code,
                    error_detail[:200],
                )
                last_error = f"HTTP {response.status_code}: {error_detail[:100]}"

            except httpx.RequestError as exc:
                logger.warning(
                    "ElevenLabs {} request error: {} — trying next key",
                    key_label,
                    exc,
                )
                last_error = str(exc)

        raise RuntimeError(
            f"All ElevenLabs API keys failed. Last error: {last_error}"
        )

    # ── Response Parsing ────────────────────────────────────────────────────

    def _parse_response(
        self,
        raw: dict[str, Any],
        speaker_detection: bool,
    ) -> list[TranscriptSegment]:
        """
        Parse ElevenLabs API response into TranscriptSegment list.

        Creates one segment per speaker turn.  Word-level timestamps are
        preserved so that Gemini can perform proper subtitle grouping
        during the translation phase.  Split words (e.g. ``"ba-"`` +
        ``"d."``) are merged at this stage.
        """
        words = raw.get("words", [])
        if not words:
            # Fallback: create a single segment from the full text
            text = raw.get("text", "")
            if text:
                return [
                    TranscriptSegment(
                        start=0.0,
                        end=1.0,
                        text=text.strip(),
                        speaker="SPEAKER_00",
                        words=[],
                    )
                ]
            return []

        # Filter to actual words and punctuation (skip spacing, audio events)
        word_entries: list[dict] = []
        for w in words:
            w_type = w.get("type", "word")
            w_text = w.get("text", "").strip()
            if not w_text:
                continue
            # Skip audio events (laughter, music, applause, etc.)
            if w_type == "audio_event":
                continue
            # Keep words and punctuation attached to words
            if w_type == "word" or (w_type == "punctuation" and word_entries):
                word_entries.append(w)

        if not word_entries:
            return []

        # ── Step 1: Attach punctuation to the preceding word's text ──
        cleaned: list[dict] = []
        for w in word_entries:
            if w.get("type") == "punctuation":
                if cleaned:
                    cleaned[-1]["text"] = cleaned[-1]["text"] + w.get("text", "")
            else:
                cleaned.append(dict(w))  # shallow copy to avoid mutating raw

        if not cleaned:
            return []

        # ── Step 2: Merge split words (e.g. "ba-" + "d." → "bad.") ──
        cleaned = self._merge_split_words(cleaned)

        # ── Step 3: Group by speaker turn or sentence boundary ──
        # Gemini will handle subtitle-level grouping during translation,
        # but splitting by sentence boundaries helps the UI show manageable blocks.
        segments: list[TranscriptSegment] = []
        current_words: list[dict] = []
        current_speaker: str | None = None

        for w in cleaned:
            w_speaker = w.get("speaker_id")
            w_text = w.get("text", "").strip()

            # Speaker change → flush current segment
            if (
                speaker_detection
                and w_speaker
                and current_speaker
                and w_speaker != current_speaker
                and current_words
            ):
                self._flush_speaker_turn(
                    segments, current_words, current_speaker, speaker_detection
                )
                current_words = []

            # Or long pause / sentence boundary → flush current segment
            elif current_words:
                prev_w = current_words[-1]
                prev_text = prev_w.get("text", "").strip()
                gap = w.get("start", w.get("end", 0.0)) - prev_w.get("end", prev_w.get("start", 0.0))
                # Split if pause > 1 second, or previous word ends with punctuation and pause > 0.3s
                if gap > 1.0 or (gap > 0.3 and prev_text.endswith((".", "?", "!"))):
                    self._flush_speaker_turn(
                        segments, current_words, current_speaker, speaker_detection
                    )
                    current_words = []

            if w_speaker:
                current_speaker = w_speaker
            current_words.append(w)

        # Flush remaining words
        if current_words:
            self._flush_speaker_turn(
                segments, current_words, current_speaker, speaker_detection
            )

        return segments

    # Floor duration applied to words that arrive with end <= start from
    # ElevenLabs Scribe.  Scribe v1 occasionally collapses short CJK runs
    # (single-mora kanji, expressive interjections) onto a single anchor
    # timestamp.  Without normalization the sanitizer's same-speaker word
    # overlap pass collapses surrounding words to that same anchor too,
    # which makes whole segments render as zero-width slivers in the
    # editor timeline.  50 ms is small enough that it never crowds out a
    # real next word, but big enough to keep the cluster-redistribution
    # pass in ``processors.timing.sanitizer`` from rounding back to zero.
    _MIN_WORD_DURATION = 0.05

    def _flush_speaker_turn(
        self,
        segments: list[TranscriptSegment],
        current_words: list[dict],
        current_speaker: str | None,
        speaker_detection: bool,
    ) -> None:
        """Build a TranscriptSegment from a single speaker turn's words."""
        wt_list: list[WordTimestamp] = []
        for cw in current_words:
            w_start = cw.get("start", 0.0)
            w_end = cw.get("end", w_start + 0.1)
            # Defensive normalization for broken ElevenLabs timestamps
            # (start == end, or in extreme cases start > end).  See the
            # comment on ``_MIN_WORD_DURATION`` above for the rationale.
            if w_end <= w_start:
                w_end = w_start + self._MIN_WORD_DURATION
            wt_list.append(
                WordTimestamp(
                    word=cw["text"].strip(),
                    start=w_start,
                    end=w_end,
                    score=1.0,
                    source="elevenlabs",
                )
            )

        if not wt_list:
            return

        text = " ".join(w.word for w in wt_list)

        # Derive segment timing from actual word timestamps
        seg_start = wt_list[0].start
        seg_end = wt_list[-1].end

        speaker = "SPEAKER_00"
        if speaker_detection and current_speaker:
            speaker = self._normalize_speaker(current_speaker)

        segments.append(
            TranscriptSegment(
                start=round(seg_start, 3),
                end=round(seg_end, 3),
                text=text,
                speaker=speaker,
                words=wt_list,
            )
        )

    @staticmethod
    def _merge_split_words(word_entries: list[dict]) -> list[dict]:
        """Merge words that were split mid-syllable by the STT engine.

        Detects words ending with a hyphen (e.g. ``"ba-"``) and merges
        them with the following word (e.g. ``"d."``), producing ``"bad."``.
        """
        if len(word_entries) < 2:
            return word_entries

        merged: list[dict] = []
        i = 0
        while i < len(word_entries):
            w = word_entries[i]
            w_text = w.get("text", "")

            # Word ending with hyphen → merge with next word
            if w_text.endswith("-") and i + 1 < len(word_entries):
                next_w = word_entries[i + 1]
                next_text = next_w.get("text", "")

                if next_text:
                    merged_entry = dict(w)
                    # Remove trailing hyphen, concatenate
                    merged_entry["text"] = w_text[:-1] + next_text
                    # End time comes from the second (merged) word
                    merged_entry["end"] = next_w.get("end", w.get("end"))
                    merged.append(merged_entry)
                    logger.debug(
                        "Merged split word: '{}' + '{}' → '{}'",
                        w_text, next_text, merged_entry["text"],
                    )
                    i += 2
                    continue

            merged.append(w)
            i += 1

        return merged

    # ── Helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _normalize_speaker(speaker_id: str) -> str:
        """Convert ElevenLabs speaker_id (e.g. 'speaker_0') to SPEAKER_00 format."""
        match = re.search(r"(\d+)", speaker_id)
        if match:
            idx = int(match.group(1))
            # ElevenLabs uses 0-based IDs (speaker_0, speaker_1, ...)
            return f"SPEAKER_{idx:02d}"
        return "SPEAKER_00"

    @staticmethod
    def _save_json(segments: list[TranscriptSegment], path: Path) -> None:
        data = [s.to_dict() for s in segments]
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
