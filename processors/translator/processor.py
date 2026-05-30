"""
processors.translator.processor — Phase 2 orchestrator.

Wires together the focused submodules:

* :mod:`.gemini_client`  — Gemini API calls (translate + regroup)
* :mod:`.regrouper`      — turn Gemini groups into ``TranscriptSegment``s
* :mod:`.recheck`        — word-level alignment recheck against ElevenLabs
* :mod:`.deepl`          — DeepL fallback translator
* :mod:`.local_grouper`  — local heuristic word→subtitle grouper

The class API is unchanged from the legacy single-file module so all
existing call sites (``main.py``, ``web/server.py``) keep working.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from loguru import logger

import config
from models.transcript import (
    TranscriptSegment,
    WordTimestamp,
    sanitize_timestamps,
)
from utils.file_utils import ensure_dir

from .claude_client import call_claude_regroup, call_claude_translate
from .constants import BATCH_SIZE, LANGUAGE_NAMES, PROMPT_VERSION, PUNCTUATION_ONLY_PATTERN
from .deepl import translate_segments_in_place, translate_texts
from .gemini_client import call_gemini_regroup, call_gemini_translate
from .local_grouper import local_group_from_segments, local_group_words
from .recheck import recheck_word_level_alignment
from .regrouper import build_word_batches, reconstruct_segments


class TranslatorProcessor:
    """
    Phase 2: Translate transcript text while preserving timing metadata.

    The translated_transcript.json retains the original start/end/speaker
    keys from source_transcript.json — only the text field changes.
    """

    def __init__(
        self,
        target_language: str = "id",
        style_preset: str = "natural",
        style_note: str | None = None,
        backend: str | None = None,
        spicy_filter: bool = False,
    ) -> None:
        self.target_language = target_language
        self.style_preset = style_preset if style_preset in {"natural", "formal"} else "natural"
        self.style_note = (style_note or "").strip() or None
        # Spicy filter: when True, the prompt asks the model to soften
        # explicit R18 vocabulary into playful equivalents AND a regex
        # post-pass catches any literal vulgar terms that slipped through.
        # See processors/translator/postprocess.py for the second layer.
        self.spicy_filter = bool(spicy_filter)
        # Backend selector. Defaults to whatever is in config.TRANSLATOR_BACKEND
        # so existing call sites (main.py, web/server.py) pick up the env var
        # without code changes. Pass an explicit value to force a backend
        # (used by the standalone test scripts).
        backend_resolved = (backend or config.TRANSLATOR_BACKEND or "gemini").strip().lower()
        if backend_resolved not in {"gemini", "claude"}:
            logger.warning(
                "Unknown translator backend '{}', falling back to 'gemini'",
                backend_resolved,
            )
            backend_resolved = "gemini"
        self.backend = backend_resolved

    # ── Public entrypoint ───────────────────────────────────────────────────

    async def translate(
        self,
        segments: list[TranscriptSegment],
        output_dir: Path | str,
        regroup: bool = False,
    ) -> tuple[list[TranscriptSegment], Path]:
        """Translate all segments and save to ``translated_transcript.json``."""
        output_dir = Path(output_dir)
        ensure_dir(output_dir)

        logger.info(
            "Translating {} segments to '{}'...",
            len(segments),
            self.target_language,
        )

        # Capture original word-level data for the recheck pass — must
        # happen BEFORE any regrouping mutates the segments list.
        original_words: list[WordTimestamp] = []
        original_speakers: list[str] = []
        for seg in segments:
            for w in seg.words:
                original_words.append(w)
                original_speakers.append(seg.speaker)

        api_keys = config.GEMINI_API_KEYS
        # When the Claude backend is selected we don't need Gemini keys at
        # all, but we still gate on a usable backend. The Claude client
        # checks NINEROUTER_API_KEY internally and returns None when missing.
        backend_ready = (
            (self.backend == "gemini" and bool(api_keys))
            or (self.backend == "claude" and bool(config.NINEROUTER_API_KEY))
        )
        if backend_ready:
            logger.info("Translator backend: {}", self.backend)
            if regroup and any(seg.words for seg in segments):
                translated = await self._translate_and_regroup(
                    segments, api_keys
                )
            else:
                translated = await self._translate_batch(segments, api_keys)
        else:
            if self.backend == "gemini":
                logger.warning("No GEMINI_API_KEYS configured — returning original text")
            else:
                logger.warning(
                    "Claude backend selected but NINEROUTER_API_KEY is missing — "
                    "returning original text",
                )
            if regroup and any(seg.words for seg in segments):
                translated = local_group_from_segments(segments)
            else:
                translated = segments

        # Recheck against ElevenLabs word-level data (catches drift).
        if original_words and any(seg.words for seg in translated):
            logger.info(
                "Running word-level recheck: {} translated segments vs {} source words",
                len(translated), len(original_words),
            )
            translated = recheck_word_level_alignment(
                translated, original_words, original_speakers,
            )

        translated = sanitize_timestamps(translated)

        # Filter punctuation-only / empty segments.
        before_filter = len(translated)
        translated = [
            seg for seg in translated
            if seg.text.strip()
            and not re.fullmatch(PUNCTUATION_ONLY_PATTERN, seg.text.strip())
        ]
        filtered_count = before_filter - len(translated)
        if filtered_count > 0:
            logger.info(
                "Removed {} punctuation-only / empty segment(s)",
                filtered_count,
            )

        json_path = output_dir / "translated_transcript.json"
        self._save_json(translated, json_path)

        # Stamp translation metadata so re-runs are auditable.
        self._save_meta(output_dir / "translation_meta.json", len(translated))

        logger.info(
            "Translation complete: {} segments → {}",
            len(translated),
            json_path,
        )
        return translated, json_path

    # ── Word-level recheck (legacy class-method alias) ──────────────────────
    #
    # Some callers reference ``TranslatorProcessor.recheck_word_level_alignment``
    # directly (e.g. ``web/server.py`` re-runs it after the user edits the
    # transcript in the preview).  Keep the staticmethod alias so those
    # callers don't need to change their import path.

    recheck_word_level_alignment = staticmethod(recheck_word_level_alignment)

    # ── Plain-text batch translation ───────────────────────────────────────

    async def _translate_batch(
        self,
        segments: list[TranscriptSegment],
        api_keys: list[str],
    ) -> list[TranscriptSegment]:
        """Translate segments in batches via the active backend."""
        lang_name = LANGUAGE_NAMES.get(self.target_language, self.target_language)
        translated_segments: list[TranscriptSegment] = []

        for batch_start in range(0, len(segments), BATCH_SIZE):
            batch = segments[batch_start: batch_start + BATCH_SIZE]
            texts = [seg.text for seg in batch]

            logger.info(
                "Translating batch {}-{} of {} segments via {}...",
                batch_start + 1,
                min(batch_start + BATCH_SIZE, len(segments)),
                len(segments),
                self.backend,
            )

            if self.backend == "claude":
                translated_texts = await call_claude_translate(
                    texts, lang_name, api_keys,
                    style_preset=self.style_preset,
                    style_note=self.style_note,
                    spicy_filter=self.spicy_filter,
                )
            else:
                translated_texts = await call_gemini_translate(
                    texts, lang_name, api_keys,
                    style_preset=self.style_preset,
                    style_note=self.style_note,
                    spicy_filter=self.spicy_filter,
                )
            if translated_texts is None:
                # Active backend exhausted → fall back to DeepL for the texts
                logger.error(
                    "{} translate failed for this batch — using DeepL fallback.",
                    self.backend.capitalize(),
                )
                translated_texts = await translate_texts(texts, self.target_language)

            # Spicy-filter post-pass: catch literal vulgar terms the model
            # ignored the prompt instruction for, and any DeepL fallback
            # output (DeepL has no R18 awareness at all).
            if self.spicy_filter and translated_texts:
                from .postprocess import apply_soft_censor_many
                translated_texts = apply_soft_censor_many(translated_texts)

            for seg, translated_text in zip(batch, translated_texts):
                translated_segments.append(
                    TranscriptSegment(
                        start=seg.start,
                        end=seg.end,
                        text=translated_text,
                        speaker=seg.speaker,
                        words=seg.words,
                        pos_x=seg.pos_x,
                        pos_y=seg.pos_y,
                        pos_override=seg.pos_override,
                    )
                )

        return translated_segments

    # ── Word-level regrouping + translation ─────────────────────────────────

    async def _translate_and_regroup(
        self,
        segments: list[TranscriptSegment],
        api_keys: list[str],
    ) -> list[TranscriptSegment]:
        """Extract word-level data, send to active backend for grouping + translation."""
        lang_name = LANGUAGE_NAMES.get(self.target_language, self.target_language)

        # Flatten words with speaker info.
        all_words: list[WordTimestamp] = []
        word_speakers: list[str] = []
        for seg in segments:
            for w in seg.words:
                all_words.append(w)
                word_speakers.append(seg.speaker)

        if not all_words:
            logger.warning(
                "No word-level data found — falling back to text-only translation"
            )
            return await self._translate_batch(segments, api_keys)

        batches = build_word_batches(all_words, word_speakers)

        all_new_segments: list[TranscriptSegment] = []
        for batch_idx, (batch_words, batch_speakers) in enumerate(batches):
            logger.info(
                "Regrouping + translating batch {}/{} ({} words) to '{}' via {}...",
                batch_idx + 1,
                len(batches),
                len(batch_words),
                lang_name,
                self.backend,
            )
            new_segs = await self._regroup_one_batch(
                batch_words, batch_speakers, lang_name, api_keys
            )
            all_new_segments.extend(new_segs)

        if not all_new_segments:
            logger.warning(
                "{} regrouping produced no segments — using fallback",
                self.backend.capitalize(),
            )
            return local_group_from_segments(segments)

        logger.info(
            "Regrouping complete: {} words → {} subtitle segments",
            len(all_words),
            len(all_new_segments),
        )
        return all_new_segments

    async def _regroup_one_batch(
        self,
        words: list[WordTimestamp],
        speakers: list[str],
        target_lang_name: str,
        api_keys: list[str],
    ) -> list[TranscriptSegment]:
        """Process a single regroup batch through the active backend + fallbacks."""
        if self.backend == "claude":
            groups, best_partial_groups = await call_claude_regroup(
                words, speakers, target_lang_name, api_keys,
                style_preset=self.style_preset,
                style_note=self.style_note,
                spicy_filter=self.spicy_filter,
            )
        else:
            groups, best_partial_groups = await call_gemini_regroup(
                words, speakers, target_lang_name, api_keys,
                style_preset=self.style_preset,
                style_note=self.style_note,
                spicy_filter=self.spicy_filter,
            )

        if groups is not None:
            segments = reconstruct_segments(groups, words, speakers)
            if segments:
                return segments
            # Reconstruction failed despite a complete response → fall through
            logger.warning("Segment reconstruction failed for full Gemini response")

        # Use partial salvaged groups + DeepL for the uncovered words.
        if best_partial_groups:
            logger.info(
                "Using {} salvaged group(s) from truncated Gemini response + "
                "local fallback for remaining words",
                len(best_partial_groups),
            )
            partial_segs = reconstruct_segments(best_partial_groups, words, speakers)
            if partial_segs:
                covered_indices: set[int] = set()
                for g in best_partial_groups:
                    for idx in g.get("indices", []):
                        if 0 <= idx < len(words):
                            covered_indices.add(idx)

                uncovered = sorted(set(range(len(words))) - covered_indices)
                if uncovered:
                    logger.info(
                        "Locally grouping {} uncovered word(s) and translating with DeepL",
                        len(uncovered),
                    )
                    unc_words = [words[i] for i in uncovered]
                    unc_speakers = [speakers[i] for i in uncovered]
                    local_segs = local_group_words(unc_words, unc_speakers)
                    translated_local = await translate_segments_in_place(
                        local_segs, self.target_language,
                    )
                    partial_segs.extend(translated_local)
                    partial_segs.sort(key=lambda s: s.start)

                return partial_segs

        # Fully exhausted: local heuristic + DeepL.
        logger.error(
            "Gemini regrouping exhausted for batch — using local fallback with DeepL.",
        )
        local_segs = local_group_words(words, speakers)
        return await translate_segments_in_place(local_segs, self.target_language)

    # ── Helpers ─────────────────────────────────────────────────────────────

    def _save_meta(self, path: Path, segment_count: int) -> None:
        """Write phase2_translation/translation_meta.json for reproducibility.

        Captures the inputs that determine the translation output: target
        language, style preset, style note, and the prompt revision tag.
        Re-running a Job with the same inputs should produce the same file.
        """
        import time

        meta = {
            "prompt_version": PROMPT_VERSION,
            "target_language": self.target_language,
            "style_preset": self.style_preset,
            "style_note": self.style_note,
            "translator_backend": self.backend,
            "translator_model_chain": list(getattr(config, "TRANSLATOR_GEMINI_FALLBACK_MODELS", []) or []),
            "translator_model_primary": (
                getattr(config, "TRANSLATOR_CLAUDE_MODEL", None)
                if self.backend == "claude"
                else getattr(config, "TRANSLATOR_GEMINI_MODEL", None)
            ),
            "segment_count": segment_count,
            "written_at": time.time(),
        }
        try:
            with path.open("w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2, ensure_ascii=False)
        except OSError as exc:
            logger.warning("Failed to write translation_meta.json: {}", exc)

    @staticmethod
    def _save_json(segments: list[TranscriptSegment], path: Path) -> None:
        data = [s.to_dict() for s in segments]
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    @staticmethod
    def load_from_json(json_path: Path | str) -> list[TranscriptSegment]:
        """Load a previously saved translated_transcript.json."""
        with Path(json_path).open("r", encoding="utf-8") as f:
            data = json.load(f)
        return [TranscriptSegment.from_dict(d) for d in data]
