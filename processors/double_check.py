"""
processors/double_check.py — Double-Check Transcript Merger.

Merges two transcripts of the same audio:
  1. YouTube auto-subs (sentence-level, often noisy but captures most words)
  2. WhisperX output (word-level timestamps, high precision, but may miss words)

The merger produces a final transcript that:
  - Uses WhisperX word-level timestamps where confidence is high
  - Falls back to auto-sub text when WhisperX confidence is low
  - Detects words present in one source but missing in the other
  - Annotates each word with a confidence source tag
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from models.transcript import TranscriptSegment, WordTimestamp


# ─── Data Classes ────────────────────────────────────────────────────────────


@dataclass
class MergedWord:
    """A single word in the merged transcript with provenance."""
    word: str
    start: float
    end: float
    confidence: float       # 0.0–1.0
    source: str             # "whisperx" | "autosub" | "both" | "interpolated"


@dataclass
class MergeStats:
    """Statistics from a merge operation."""
    total_words: int = 0
    whisperx_only: int = 0      # Words only in WhisperX
    autosub_only: int = 0       # Words only in auto-subs (recovered)
    both_agree: int = 0         # Words present in both, text matches
    whisperx_preferred: int = 0 # Words in both but different, WhisperX chosen
    autosub_preferred: int = 0  # Words in both but different, auto-sub chosen
    interpolated: int = 0       # Words with interpolated timestamps
    llm_corrected: int = 0      # Words corrected by LLM review


# ─── Noise Filter ────────────────────────────────────────────────────────────

_AUTOSUB_NOISE = re.compile(
    r"^\[.*\]$"       # [Music], [Applause], [Laughter]
    r"|^<.*>$"        # <i>, </i>, etc.
    r"|^♪+$"          # Music notes
    r"|^[\W]$",       # Single punctuation
    re.IGNORECASE,
)


# ─── Core Merger ─────────────────────────────────────────────────────────────


class DoubleCheckMerger:
    """Merges auto-sub and WhisperX transcripts for higher accuracy."""

    def __init__(
        self,
        whisperx_min_confidence: float = 0.5,
        similarity_threshold: float = 0.6,
    ):
        self.whisperx_min_confidence = whisperx_min_confidence
        self.similarity_threshold = similarity_threshold

    # ── Public API ───────────────────────────────────────────────────────────

    def merge(
        self,
        autosub_segments: list[dict],
        whisperx_segments: list[TranscriptSegment],
    ) -> tuple[list[TranscriptSegment], MergeStats]:
        """Merge auto-sub and WhisperX transcripts.

        Parameters
        ----------
        autosub_segments : list[dict]
            Sliced auto-sub segments: [{start, end, text}, ...]
            Timestamps are relative to clip start (0-based).
        whisperx_segments : list[TranscriptSegment]
            WhisperX output with word-level timestamps.

        Returns
        -------
        tuple[list[TranscriptSegment], MergeStats]
            Merged segments and merge statistics.
        """
        stats = MergeStats()

        # Stage 1: Tokenize both sources
        autosub_words = self._tokenize_autosub(autosub_segments)
        whisperx_words = self._extract_whisperx_words(whisperx_segments)

        # Edge case: one source is empty
        if not autosub_words and not whisperx_words:
            return [], stats
        if not autosub_words:
            logger.info("Double-Check: No auto-subs, using WhisperX only")
            return whisperx_segments, stats
        if not whisperx_words:
            logger.info("Double-Check: No WhisperX output, converting auto-subs")
            segments = self._autosub_to_segments(autosub_segments)
            stats.autosub_only = sum(len(s.text.split()) for s in segments)
            stats.total_words = stats.autosub_only
            return segments, stats

        # Stage 2: Align word sequences
        ops = self._align_word_sequences(autosub_words, whisperx_words)

        # Stage 3: Reconcile each aligned chunk
        merged_words: list[MergedWord] = []
        for tag, a_chunk, w_chunk in ops:
            merged_words.extend(
                self._reconcile_chunk(tag, a_chunk, w_chunk, stats)
            )

        stats.total_words = len(merged_words)

        # Check overlap ratio and warn if transcripts are very different
        if stats.total_words > 0:
            overlap_ratio = stats.both_agree / stats.total_words
            if overlap_ratio < 0.3:
                logger.warning(
                    "Double-Check: Low overlap ratio ({:.1%}). "
                    "Auto-subs and WhisperX may be very different.",
                    overlap_ratio,
                )

        # Stage 4: Interpolate timestamps for autosub-only words
        self._interpolate_missing_timestamps(merged_words, stats)

        # Stage 5: Rebuild into TranscriptSegment list
        segments = self._rebuild_segments(merged_words, whisperx_segments)

        return segments, stats

    @staticmethod
    def load_autosub_json(path: Path) -> list[dict]:
        """Load a clip_NNN_autosub.json file."""
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)

    # ── Stage 1: Tokenize ────────────────────────────────────────────────────

    @staticmethod
    def _normalize_text(text: str) -> str:
        """Lowercase, strip punctuation, collapse whitespace."""
        text = text.lower().strip()
        text = re.sub(r"[^\w\s']", "", text)  # Keep apostrophes for contractions
        text = re.sub(r"\s+", " ", text)
        return text

    def _tokenize_autosub(self, segments: list[dict]) -> list[dict]:
        """Convert sentence-level auto-subs into pseudo-word-level tokens.

        Each word gets a linearly interpolated timestamp within its segment.
        """
        tokens: list[dict] = []

        for seg_idx, seg in enumerate(segments):
            text = seg.get("text", "").strip()
            if not text:
                continue

            words = text.split()
            if not words:
                continue

            seg_start = seg["start"]
            seg_end = seg["end"]
            seg_duration = seg_end - seg_start

            if seg_duration <= 0:
                seg_duration = 0.1 * len(words)

            per_word = seg_duration / len(words)

            for i, word in enumerate(words):
                tokens.append({
                    "word": word,
                    "start": round(seg_start + i * per_word, 3),
                    "end": round(seg_start + (i + 1) * per_word, 3),
                    "score": 0.0,  # No confidence from auto-subs
                    "seg_idx": seg_idx,
                })

        return tokens

    @staticmethod
    def _extract_whisperx_words(segments: list[TranscriptSegment]) -> list[dict]:
        """Flatten WhisperX segments into a flat word list."""
        tokens: list[dict] = []

        for seg_idx, seg in enumerate(segments):
            for w in seg.words:
                tokens.append({
                    "word": w.word,
                    "start": w.start,
                    "end": w.end,
                    "score": w.score,
                    "seg_idx": seg_idx,
                })

        return tokens

    # ── Stage 2: Align ───────────────────────────────────────────────────────

    def _align_word_sequences(
        self,
        autosub_words: list[dict],
        whisperx_words: list[dict],
    ) -> list[tuple[str, list[dict], list[dict]]]:
        """Align two word sequences using SequenceMatcher.

        Returns a list of alignment operations:
          ("equal", autosub_chunk, whisperx_chunk)
          ("replace", autosub_chunk, whisperx_chunk)
          ("insert", [], whisperx_chunk)
          ("delete", autosub_chunk, [])
        """
        autosub_normalized = [
            self._normalize_text(w["word"]) for w in autosub_words
        ]
        whisperx_normalized = [
            self._normalize_text(w["word"]) for w in whisperx_words
        ]

        matcher = difflib.SequenceMatcher(
            None, autosub_normalized, whisperx_normalized
        )

        ops: list[tuple[str, list[dict], list[dict]]] = []
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            a_chunk = autosub_words[i1:i2]
            w_chunk = whisperx_words[j1:j2]
            ops.append((tag, a_chunk, w_chunk))

        return ops

    # ── Stage 3: Reconcile ───────────────────────────────────────────────────

    def _reconcile_chunk(
        self,
        tag: str,
        autosub_chunk: list[dict],
        whisperx_chunk: list[dict],
        stats: MergeStats,
    ) -> list[MergedWord]:
        """Decide which source(s) to use for an aligned chunk."""
        result: list[MergedWord] = []

        if tag == "equal":
            # Words match — use WhisperX timestamps (more precise)
            for a_word, w_word in zip(autosub_chunk, whisperx_chunk):
                result.append(MergedWord(
                    word=w_word["word"],  # Use WhisperX capitalization
                    start=w_word["start"],
                    end=w_word["end"],
                    confidence=max(w_word.get("score", 0.0), 0.8),
                    source="both",
                ))
                stats.both_agree += 1

        elif tag == "replace":
            # Different words at same position
            result.extend(
                self._reconcile_replace(autosub_chunk, whisperx_chunk, stats)
            )

        elif tag == "insert":
            # Words only in WhisperX (not in auto-subs)
            for w_word in whisperx_chunk:
                result.append(MergedWord(
                    word=w_word["word"],
                    start=w_word["start"],
                    end=w_word["end"],
                    confidence=w_word.get("score", 0.5),
                    source="whisperx",
                ))
                stats.whisperx_only += 1

        elif tag == "delete":
            # Words only in auto-subs (not in WhisperX)
            for a_word in autosub_chunk:
                if self._is_autosub_noise(a_word["word"]):
                    continue  # Skip noise tokens

                result.append(MergedWord(
                    word=a_word["word"],
                    start=a_word["start"],  # Will be interpolated later
                    end=a_word["end"],
                    confidence=0.3,  # Lower confidence for autosub-only
                    source="autosub",
                ))
                stats.autosub_only += 1

        return result

    def _reconcile_replace(
        self,
        autosub_chunk: list[dict],
        whisperx_chunk: list[dict],
        stats: MergeStats,
    ) -> list[MergedWord]:
        """Handle 'replace' opcode — different words at same position."""
        result: list[MergedWord] = []

        # If chunks are same length, compare word-by-word
        if len(autosub_chunk) == len(whisperx_chunk):
            for a_word, w_word in zip(autosub_chunk, whisperx_chunk):
                a_norm = self._normalize_text(a_word["word"])
                w_norm = self._normalize_text(w_word["word"])

                # Check if words are very similar (minor spelling diff)
                ratio = difflib.SequenceMatcher(None, a_norm, w_norm).ratio()
                w_score = w_word.get("score", 0.0)

                if ratio > 0.8:
                    # Very similar — treat as match, use WhisperX
                    result.append(MergedWord(
                        word=w_word["word"],
                        start=w_word["start"],
                        end=w_word["end"],
                        confidence=max(w_score, 0.7),
                        source="both",
                    ))
                    stats.both_agree += 1
                elif w_score >= self.whisperx_min_confidence:
                    # WhisperX confident — trust it
                    result.append(MergedWord(
                        word=w_word["word"],
                        start=w_word["start"],
                        end=w_word["end"],
                        confidence=w_score,
                        source="whisperx",
                    ))
                    stats.whisperx_preferred += 1
                else:
                    # WhisperX low confidence — use auto-sub text
                    # but still use WhisperX timing (more precise)
                    result.append(MergedWord(
                        word=a_word["word"],
                        start=w_word["start"],
                        end=w_word["end"],
                        confidence=0.4,
                        source="autosub",
                    ))
                    stats.autosub_preferred += 1
        else:
            # Different lengths — include both sets
            # Prefer WhisperX words if confident
            w_avg_score = (
                sum(w.get("score", 0.0) for w in whisperx_chunk) / len(whisperx_chunk)
                if whisperx_chunk else 0.0
            )

            if w_avg_score >= self.whisperx_min_confidence:
                # Trust WhisperX version
                for w_word in whisperx_chunk:
                    result.append(MergedWord(
                        word=w_word["word"],
                        start=w_word["start"],
                        end=w_word["end"],
                        confidence=w_word.get("score", 0.5),
                        source="whisperx",
                    ))
                    stats.whisperx_preferred += 1
            else:
                # Low WhisperX confidence — use auto-sub version
                # but use WhisperX time range for interpolation
                time_start = (
                    whisperx_chunk[0]["start"] if whisperx_chunk
                    else autosub_chunk[0]["start"]
                )
                time_end = (
                    whisperx_chunk[-1]["end"] if whisperx_chunk
                    else autosub_chunk[-1]["end"]
                )
                duration = time_end - time_start
                per_word = duration / len(autosub_chunk) if autosub_chunk else 0

                for i, a_word in enumerate(autosub_chunk):
                    result.append(MergedWord(
                        word=a_word["word"],
                        start=round(time_start + i * per_word, 3),
                        end=round(time_start + (i + 1) * per_word, 3),
                        confidence=0.35,
                        source="autosub",
                    ))
                    stats.autosub_preferred += 1

        return result

    # ── Stage 4: Interpolate timestamps ──────────────────────────────────────

    @staticmethod
    def _interpolate_missing_timestamps(
        merged_words: list[MergedWord],
        stats: MergeStats,
    ) -> None:
        """Fix timestamps for autosub-only words using surrounding context.

        For words sourced only from auto-subs, their timestamps are from
        linear interpolation within the original sentence. If we have
        better timing anchors from adjacent WhisperX words, use those.
        """
        if not merged_words:
            return

        for i, mw in enumerate(merged_words):
            if mw.source != "autosub":
                continue

            # Find nearest preceding non-autosub word
            prev_end = None
            for j in range(i - 1, -1, -1):
                if merged_words[j].source != "autosub":
                    prev_end = merged_words[j].end
                    break

            # Find nearest following non-autosub word
            next_start = None
            for j in range(i + 1, len(merged_words)):
                if merged_words[j].source != "autosub":
                    next_start = merged_words[j].start
                    break

            # Count consecutive autosub-only words in this gap
            gap_start_idx = i
            while gap_start_idx > 0 and merged_words[gap_start_idx - 1].source == "autosub":
                gap_start_idx -= 1
            gap_end_idx = i
            while gap_end_idx < len(merged_words) - 1 and merged_words[gap_end_idx + 1].source == "autosub":
                gap_end_idx += 1

            total_in_gap = gap_end_idx - gap_start_idx + 1
            pos_in_gap = i - gap_start_idx

            if prev_end is not None and next_start is not None and next_start > prev_end:
                gap_duration = next_start - prev_end
                per_word = gap_duration / total_in_gap
                mw.start = round(prev_end + pos_in_gap * per_word, 3)
                mw.end = round(prev_end + (pos_in_gap + 1) * per_word, 3)
                mw.source = "interpolated"
                stats.interpolated += 1

    # ── Stage 5: Rebuild segments ────────────────────────────────────────────

    @staticmethod
    def _rebuild_segments(
        merged_words: list[MergedWord],
        reference_segments: list[TranscriptSegment],
    ) -> list[TranscriptSegment]:
        """Group merged words back into TranscriptSegment list.

        Uses the WhisperX segment boundaries as the primary guide.
        """
        if not merged_words:
            return []

        if not reference_segments:
            # No reference segments — create one big segment
            words = [
                WordTimestamp(
                    word=mw.word,
                    start=mw.start,
                    end=mw.end,
                    score=mw.confidence,
                    source=mw.source,
                )
                for mw in merged_words
            ]
            text = " ".join(mw.word for mw in merged_words)
            return [TranscriptSegment(
                start=merged_words[0].start,
                end=merged_words[-1].end,
                text=text,
                speaker="SPEAKER_00",
                words=words,
            )]

        # Build segment bins based on reference boundaries
        bins: list[list[MergedWord]] = [[] for _ in reference_segments]
        orphans: list[MergedWord] = []

        for mw in merged_words:
            placed = False
            for seg_idx, seg in enumerate(reference_segments):
                # Word belongs to this segment if its midpoint falls within
                word_mid = (mw.start + mw.end) / 2
                if seg.start <= word_mid <= seg.end:
                    bins[seg_idx].append(mw)
                    placed = True
                    break

            if not placed:
                orphans.append(mw)

        # Assign orphans to nearest segment
        for mw in orphans:
            word_mid = (mw.start + mw.end) / 2
            best_idx = 0
            best_dist = abs(word_mid - reference_segments[0].start)
            for seg_idx, seg in enumerate(reference_segments):
                seg_mid = (seg.start + seg.end) / 2
                dist = abs(word_mid - seg_mid)
                if dist < best_dist:
                    best_dist = dist
                    best_idx = seg_idx
            bins[best_idx].append(mw)

        # Sort each bin by start time
        for b in bins:
            b.sort(key=lambda mw: mw.start)

        # Build final segments
        result: list[TranscriptSegment] = []
        for seg_idx, (ref_seg, bin_words) in enumerate(zip(reference_segments, bins)):
            if not bin_words:
                continue

            words = [
                WordTimestamp(
                    word=mw.word,
                    start=mw.start,
                    end=mw.end,
                    score=mw.confidence,
                    source=mw.source,
                )
                for mw in bin_words
            ]
            text = " ".join(mw.word for mw in bin_words)

            result.append(TranscriptSegment(
                start=bin_words[0].start,
                end=bin_words[-1].end,
                text=text,
                speaker=ref_seg.speaker,
                words=words,
            ))

        return result

    # ── Helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _is_autosub_noise(word: str) -> bool:
        """Check if a word is auto-sub noise (non-speech annotation)."""
        return bool(_AUTOSUB_NOISE.match(word.strip()))

    @staticmethod
    def _autosub_to_segments(autosub_segments: list[dict]) -> list[TranscriptSegment]:
        """Convert raw auto-sub dicts to TranscriptSegment list (fallback)."""
        result: list[TranscriptSegment] = []
        for seg in autosub_segments:
            text = seg.get("text", "").strip()
            if not text:
                continue

            # Create word-level tokens via interpolation
            words_text = text.split()
            seg_start = seg["start"]
            seg_end = seg["end"]
            duration = seg_end - seg_start
            per_word = duration / len(words_text) if words_text else 0

            words = [
                WordTimestamp(
                    word=w,
                    start=round(seg_start + i * per_word, 3),
                    end=round(seg_start + (i + 1) * per_word, 3),
                    score=0.0,
                    source="autosub",
                )
                for i, w in enumerate(words_text)
            ]

            result.append(TranscriptSegment(
                start=seg_start,
                end=seg_end,
                text=text,
                speaker="SPEAKER_00",
                words=words,
            ))

        return result

    # ── LLM Review (Stage 6 — optional) ──────────────────────────────────────

    async def llm_review(
        self,
        merged_segments: list[TranscriptSegment],
        api_keys: list[str],
        log_fn: Callable[[str], None] | None = None,
        debug_dir: Path | None = None,
    ) -> tuple[list[TranscriptSegment], int]:
        """Review conflict words using Gemini LLM for context-aware correction.

        Only words with source != "both" are sent to LLM for review.
        This is a non-fatal step — if LLM fails, the original merge is kept.

        Parameters
        ----------
        merged_segments : list[TranscriptSegment]
            Output from merge() with word-level provenance.
        api_keys : list[str]
            Gemini API keys for rotation.
        log_fn : Callable, optional
            Logging callback.
        debug_dir : Path, optional
            Directory to save debug files (llm_conflicts.json, llm_response.json).

        Returns
        -------
        tuple[list[TranscriptSegment], int]
            Updated segments and count of LLM corrections.
        """
        def log(msg: str) -> None:
            if log_fn:
                log_fn(msg)

        # Step 1: Collect conflict words
        conflicts = self._collect_conflicts(merged_segments)

        if not conflicts:
            log("  No conflict words to review — skipping LLM")
            return merged_segments, 0

        log(f"  Found {len(conflicts)} conflict words for LLM review")

        # Save conflicts debug file
        if debug_dir:
            debug_dir.mkdir(parents=True, exist_ok=True)
            with (debug_dir / "llm_conflicts.json").open("w", encoding="utf-8") as f:
                json.dump(conflicts, f, ensure_ascii=False, indent=2)

        # Step 2: Build prompt and call Gemini (batch if >50 conflicts)
        total_corrected = 0
        batch_size = 50
        all_responses: list[dict] = []

        for batch_start in range(0, len(conflicts), batch_size):
            batch = conflicts[batch_start:batch_start + batch_size]
            prompt = self._build_review_prompt(merged_segments, batch)

            response_text = await self._call_gemini(prompt, api_keys, log_fn)
            if not response_text:
                log("  LLM call failed — keeping programmatic merge")
                all_responses.append({
                    "batch_start": batch_start,
                    "batch_size": len(batch),
                    "status": "failed",
                    "response": None,
                })
                continue

            # Save raw response for debug
            all_responses.append({
                "batch_start": batch_start,
                "batch_size": len(batch),
                "status": "ok",
                "response": response_text,
            })

            # Step 3: Parse and apply corrections
            try:
                corrections = json.loads(response_text)
                if not isinstance(corrections, list):
                    corrections = []
            except (json.JSONDecodeError, ValueError):
                # Try to extract JSON array from response
                match = re.search(r"\[.*\]", response_text, re.DOTALL)
                if match:
                    try:
                        corrections = json.loads(match.group(0))
                    except (json.JSONDecodeError, ValueError):
                        corrections = []
                else:
                    corrections = []

            corrected = self._apply_corrections(merged_segments, batch, corrections)
            total_corrected += corrected

        # Save LLM responses debug file
        if debug_dir:
            with (debug_dir / "llm_response.json").open("w", encoding="utf-8") as f:
                json.dump(all_responses, f, ensure_ascii=False, indent=2)

        return merged_segments, total_corrected

    def _collect_conflicts(
        self,
        segments: list[TranscriptSegment],
    ) -> list[dict]:
        """Collect words that need LLM review (source != 'both')."""
        conflicts: list[dict] = []

        # Build a flat word list with segment/word indices for context
        all_words: list[tuple[int, int, WordTimestamp]] = []
        for seg_idx, seg in enumerate(segments):
            for word_idx, w in enumerate(seg.words):
                all_words.append((seg_idx, word_idx, w))

        for flat_idx, (seg_idx, word_idx, w) in enumerate(all_words):
            if w.source == "both":
                continue

            # Build context: ~5 words before and after
            ctx_before_words = []
            for j in range(max(0, flat_idx - 5), flat_idx):
                ctx_before_words.append(all_words[j][2].word)
            ctx_after_words = []
            for j in range(flat_idx + 1, min(len(all_words), flat_idx + 6)):
                ctx_after_words.append(all_words[j][2].word)

            conflicts.append({
                "seg_idx": seg_idx,
                "word_idx": word_idx,
                "word": w.word,
                "source": w.source,
                "confidence": w.score,
                "context_before": " ".join(ctx_before_words),
                "context_after": " ".join(ctx_after_words),
            })

        return conflicts

    def _build_review_prompt(
        self,
        segments: list[TranscriptSegment],
        conflicts: list[dict],
    ) -> str:
        """Build Gemini prompt with conflict words marked in context."""
        lines = [
            "You are a transcript proofreader. Below is a transcript with uncertain words.",
            "Words marked with [?N] are uncertain — they came from only one ASR source.",
            "For each uncertain word, decide the CORRECT word based on sentence context.",
            "",
            "TRANSCRIPT WITH MARKED WORDS:",
        ]

        # Group conflicts by segment for readable context
        seg_conflicts: dict[int, list[tuple[int, dict]]] = {}
        for i, c in enumerate(conflicts):
            seg_idx = c["seg_idx"]
            if seg_idx not in seg_conflicts:
                seg_conflicts[seg_idx] = []
            seg_conflicts[seg_idx].append((i, c))

        for seg_idx in sorted(seg_conflicts.keys()):
            seg = segments[seg_idx]
            # Build segment text with markers
            words_display: list[str] = []
            conflict_word_indices = {c["word_idx"]: (i, c) for i, c in seg_conflicts[seg_idx]}

            for w_idx, w in enumerate(seg.words):
                if w_idx in conflict_word_indices:
                    global_idx, conflict = conflict_word_indices[w_idx]
                    words_display.append(f"[?{global_idx}:{w.word}]")
                else:
                    words_display.append(w.word)

            seg_text = " ".join(words_display)
            lines.append(f'  ({seg.start:.1f}s-{seg.end:.1f}s): "{seg_text}"')

        lines.extend([
            "",
            "INSTRUCTIONS:",
            "- For each [?N:word] marker, determine if the word is correct or needs fixing.",
            "- Consider grammar, context, and common speech patterns.",
            "- If the word is already correct, return it unchanged.",
            "- Return ONLY a valid JSON array with your decisions.",
            "",
            "Return format:",
            '[{"index": 0, "correct": "the_correct_word"}, ...]',
        ])

        return "\n".join(lines)

    @staticmethod
    async def _call_gemini(
        prompt: str,
        api_keys: list[str],
        log_fn: Callable[[str], None] | None = None,
    ) -> str | None:
        """Call Gemini API with key rotation. Returns response text or None."""
        import httpx

        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.1,
                "maxOutputTokens": 8192,
                "responseMimeType": "application/json",
            },
        }

        last_error = None
        for key_idx, api_key in enumerate(api_keys):
            key_label = f"Key #{key_idx + 1}"
            try:
                url = (
                    "https://generativelanguage.googleapis.com/v1beta/models/"
                    f"gemini-2.5-flash:generateContent?key={api_key}"
                )
                async with httpx.AsyncClient(timeout=None) as client:
                    response = await client.post(url, json=payload)

                if response.status_code in (429, 403):
                    if log_fn:
                        log_fn(f"  LLM {key_label} rate-limited (HTTP {response.status_code}), trying next...")
                    last_error = f"HTTP {response.status_code}"
                    continue

                if response.status_code != 200:
                    logger.warning("Gemini LLM review error (HTTP {}): {}", response.status_code, response.text[:300])
                    last_error = f"HTTP {response.status_code}"
                    continue

                result = response.json()
                candidates = result.get("candidates", [])
                if not candidates:
                    logger.warning("Gemini LLM review returned no candidates")
                    last_error = "no candidates"
                    continue

                content = candidates[0].get("content", {})
                parts = content.get("parts", [])
                text = parts[0].get("text", "") if parts else ""

                if text:
                    return text

                last_error = "empty response text"

            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                err_type = "timeout" if isinstance(exc, httpx.TimeoutException) else "connection error"
                if log_fn:
                    log_fn(f"  LLM {key_label} {err_type}, trying next...")
                last_error = f"{err_type}: {exc}"
                continue
            except Exception as exc:
                logger.warning("Gemini LLM review unexpected error: {}", exc)
                last_error = str(exc)
                continue

        logger.warning("All Gemini keys exhausted for LLM review: {}", last_error)
        return None

    @staticmethod
    def _apply_corrections(
        segments: list[TranscriptSegment],
        conflicts: list[dict],
        corrections: list[dict],
    ) -> int:
        """Apply LLM corrections to segments. Returns count of actual corrections."""
        corrected = 0

        for correction in corrections:
            if not isinstance(correction, dict):
                continue

            idx = correction.get("index")
            correct_word = correction.get("correct", "")

            if idx is None or not correct_word or not isinstance(idx, int):
                continue
            if idx < 0 or idx >= len(conflicts):
                continue

            conflict = conflicts[idx]
            seg_idx = conflict["seg_idx"]
            word_idx = conflict["word_idx"]

            if seg_idx >= len(segments):
                continue
            seg = segments[seg_idx]
            if word_idx >= len(seg.words):
                continue

            old_word = seg.words[word_idx].word

            # Only count as correction if the word actually changed
            if old_word.lower().strip() != correct_word.lower().strip():
                seg.words[word_idx].word = correct_word
                seg.words[word_idx].source = "llm_corrected"
                corrected += 1

        # Rebuild segment text from words
        for seg in segments:
            seg.text = " ".join(w.word for w in seg.words)

        return corrected
