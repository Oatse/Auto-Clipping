"""
processors/translator.py — Phase 2: Translation with Duration Awareness.

This module provides translation via Gemini API that:
  - Preserves original start/end/speaker timestamps.
  - Replaces only the text field with the translated version.
  - Saves output to translated_transcript.json.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
from loguru import logger

import config
from models.transcript import TranscriptSegment, WordTimestamp, sanitize_timestamps
from utils.file_utils import ensure_dir

# Language name mapping for clearer prompts
LANGUAGE_NAMES: dict[str, str] = {
    "en": "English",
    "id": "Indonesian",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "ja": "Japanese",
    "ko": "Korean",
    "zh": "Chinese",
    "ar": "Arabic",
    "pt": "Portuguese",
    "ru": "Russian",
    "hi": "Hindi",
    "th": "Thai",
    "vi": "Vietnamese",
    "tr": "Turkish",
    "it": "Italian",
    "nl": "Dutch",
    "pl": "Polish",
}

GEMINI_API_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-3-flash-preview:generateContent"
)

# Translate in batches to avoid token limits
BATCH_SIZE = 30


class TranslatorProcessor:
    """
    Phase 2: Translate transcript text while preserving timing metadata.

    The translated_transcript.json retains the original start/end/speaker
    keys from source_transcript.json — only the text field changes.
    """

    def __init__(self, target_language: str = "id") -> None:
        self.target_language = target_language

    async def translate(
        self,
        segments: list[TranscriptSegment],
        output_dir: Path | str,
        regroup: bool = False,
    ) -> tuple[list[TranscriptSegment], Path]:
        """
        Translate all segments and save to translated_transcript.json.

        Parameters
        ----------
        segments:
            Source transcript segments from Phase 1.
        output_dir:
            Directory to save translated_transcript.json.

        Returns
        -------
        tuple[list[TranscriptSegment], Path]
            Translated segments and path to translated_transcript.json.
        """
        output_dir = Path(output_dir)
        ensure_dir(output_dir)

        logger.info(
            "Translating {} segments to '{}'...",
            len(segments),
            self.target_language,
        )

        # Collect original word-level data for recheck (before any regrouping)
        original_words: list[WordTimestamp] = []
        original_speakers: list[str] = []
        for seg in segments:
            for w in seg.words:
                original_words.append(w)
                original_speakers.append(seg.speaker)

        # Check if Gemini API keys are available
        api_keys = config.GEMINI_API_KEYS
        if api_keys:
            if regroup and any(seg.words for seg in segments):
                translated = await self._translate_and_regroup_gemini(
                    segments, api_keys
                )
            else:
                translated = await self._translate_batch_gemini(segments, api_keys)
        else:
            logger.warning("No GEMINI_API_KEYS configured — returning original text")
            if regroup and any(seg.words for seg in segments):
                translated = self._local_group_from_segments(segments)
            else:
                translated = segments

        # ── Recheck translated segments against ElevenLabs word-level data ──
        # This catches timing drift, missing words, duplicate assignments,
        # and segment boundary misalignment introduced by Gemini regrouping.
        if original_words and any(seg.words for seg in translated):
            logger.info(
                "Running word-level recheck: {} translated segments vs {} source words",
                len(translated), len(original_words),
            )
            translated = self.recheck_word_level_alignment(
                translated, original_words, original_speakers,
            )

        # Sanitize timestamps to fix any same-speaker overlaps introduced
        # during translation/regrouping.
        translated = sanitize_timestamps(translated)

        # ── Filter out punctuation-only / empty segments ───────────────────
        # Gemini can sometimes produce segments whose translated text is just
        # punctuation (e.g. ".", "!", "?"). These create visual artifacts in
        # the subtitle output and should be removed.
        before_filter = len(translated)
        translated = [
            seg for seg in translated
            if seg.text.strip() and not re.fullmatch(r'[\s\.\!\?\,\;\:\-\—\–\…\"\'«»""'']+', seg.text.strip())
        ]
        filtered_count = before_filter - len(translated)
        if filtered_count > 0:
            logger.info(
                "Removed {} punctuation-only / empty segment(s)",
                filtered_count,
            )

        json_path = output_dir / "translated_transcript.json"
        self._save_json(translated, json_path)

        logger.info(
            "Translation complete: {} segments → {}",
            len(translated),
            json_path,
        )
        return translated, json_path

    async def _translate_batch_gemini(
        self,
        segments: list[TranscriptSegment],
        api_keys: list[str],
    ) -> list[TranscriptSegment]:
        """Translate segments in batches using Gemini API."""
        lang_name = LANGUAGE_NAMES.get(self.target_language, self.target_language)
        translated_segments: list[TranscriptSegment] = []

        for batch_start in range(0, len(segments), BATCH_SIZE):
            batch = segments[batch_start : batch_start + BATCH_SIZE]
            texts = [seg.text for seg in batch]

            logger.info(
                "Translating batch {}-{} of {} segments...",
                batch_start + 1,
                min(batch_start + BATCH_SIZE, len(segments)),
                len(segments),
            )

            translated_texts = await self._call_gemini_translate(
                texts, lang_name, api_keys
            )

            # Map translations back to segments
            for seg, translated_text in zip(batch, translated_texts):
                translated_segments.append(
                    TranscriptSegment(
                        start=seg.start,
                        end=seg.end,
                        text=translated_text,
                        speaker=seg.speaker,
                        words=seg.words,  # Preserve word-level timestamps
                        pos_x=seg.pos_x,
                        pos_y=seg.pos_y,
                        pos_override=seg.pos_override,
                    )
                )

        return translated_segments

    async def _call_gemini_translate(
        self,
        texts: list[str],
        target_lang_name: str,
        api_keys: list[str],
    ) -> list[str]:
        """
        Call Gemini API to translate a batch of texts.

        Returns translated texts in the same order. If translation fails,
        returns original texts as fallback.
        """
        # Build numbered text list for the prompt
        numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(texts))

        payload = {
            "contents": [
                {
                    "parts": [
                        {
                            "text": (
                                f"Translate the following numbered lines into {target_lang_name}. "
                                "Return ONLY a JSON array of translated strings in the same order. "
                                "Preserve the meaning and natural speech style. "
                                "If a word or phrase contains stretched/elongated characters that represent "
                                "emotional emphasis (e.g. 'noooooo', 'BAKAAAAAAA', 'stoppppp'), "
                                "translate it AND preserve that elongation style using the equivalent "
                                "stretched form in the target language. "
                                "IMPORTANT — do NOT translate lines that consist entirely of expressive "
                                "vocalizations, exclamations, onomatopoeia, or romanized non-source-language "
                                "sounds that carry no direct lexical meaning. Keep those lines EXACTLY as-is. "
                                "Examples of lines to keep unchanged: purely expressive Japanese/Asian romaji "
                                "sounds ('soryaaaa', 'kyaaaa', 'uwaaaa', 'yataaa', 'ikuzoooo', 'iyaaaaa'), "
                                "universal vocal sounds ('ahhhh', 'ohhhh', 'ehhhh', 'ahahaha', 'ufufufu'), "
                                "and sound effects ('zuuun', 'dodododo', 'baaaaam'). "
                                "Do NOT add numbering in the output, just the translated text in a JSON array.\n\n"
                                f"{numbered}"
                            )
                        }
                    ]
                }
            ],
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": 8192,
                "responseMimeType": "application/json",
            },
        }

        last_error = None
        for key_idx, api_key in enumerate(api_keys):
            try:
                url = f"{GEMINI_API_URL}?key={api_key}"

                async with httpx.AsyncClient(timeout=120.0) as client:
                    response = await client.post(url, json=payload)

                if response.status_code in (429, 403):
                    logger.warning(
                        "Gemini Key #{} rate-limited (HTTP {}), trying next...",
                        key_idx + 1,
                        response.status_code,
                    )
                    last_error = f"HTTP {response.status_code}"
                    continue

                if response.status_code != 200:
                    logger.error(
                        "Gemini API error (HTTP {}): {}",
                        response.status_code,
                        response.text[:300],
                    )
                    last_error = f"HTTP {response.status_code}"
                    continue

                result = response.json()
                candidates = result.get("candidates", [])
                if not candidates:
                    logger.warning("Gemini returned no candidates")
                    last_error = "No candidates"
                    continue

                content = candidates[0].get("content", {})
                parts = content.get("parts", [])
                raw_text = parts[0].get("text", "") if parts else ""

                # Parse JSON array from response
                translated = json.loads(raw_text)
                if isinstance(translated, list) and len(translated) == len(texts):
                    return [str(t) for t in translated]

                # Length mismatch — try to salvage
                if isinstance(translated, list):
                    logger.warning(
                        "Translation count mismatch: got {} expected {}",
                        len(translated),
                        len(texts),
                    )
                    # Pad or truncate
                    result_list = [str(t) for t in translated]
                    while len(result_list) < len(texts):
                        result_list.append(texts[len(result_list)])
                    return result_list[: len(texts)]

                logger.warning("Unexpected Gemini response format: {}", raw_text[:200])
                last_error = "Unexpected response format"
                continue

            except json.JSONDecodeError as exc:
                logger.warning("Failed to parse Gemini JSON response: {}", exc)
                last_error = f"JSON parse error: {exc}"
                continue

            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                logger.warning("Gemini Key #{} network error: {}", key_idx + 1, exc)
                last_error = str(exc)
                continue

        logger.error(
            "All Gemini API keys failed for translation. Last error: {}. "
            "Falling back to DeepL API.",
            last_error,
        )
        return await self._translate_texts_deepl_fallback(texts)

    async def _translate_texts_deepl_fallback(
        self,
        texts: list[str]
    ) -> list[str]:
        """Translate a list of strings using DeepL API when Gemini fails."""
        if not texts:
            return texts

        if not config.DEEPL_API_KEY:
            logger.warning(
                "DeepL fallback skipped: DEEPL_API_KEY not configured. "
                "Returning source-language texts as-is."
            )
            return texts

        logger.info("Starting DeepL fallback for text-only translation ({} items)...", len(texts))

        target_code = self.target_language.upper()
        if target_code == "EN":
            target_code = "EN-US"
        elif target_code == "PT":
            target_code = "PT-PT"

        url = "https://api-free.deepl.com/v2/translate"
        headers = {
            "Authorization": f"DeepL-Auth-Key {config.DEEPL_API_KEY}",
            "Content-Type": "application/json"
        }
        
        result_texts = list(texts)
        batch_size = 50
        import httpx

        for i in range(0, len(texts), batch_size):
            batch_texts = texts[i:i + batch_size]

            payload = {
                "text": batch_texts,
                "target_lang": target_code
            }

            try:
                async with httpx.AsyncClient(timeout=60.0) as client:
                    response = await client.post(url, headers=headers, json=payload)
                
                if response.status_code == 200:
                    data = response.json()
                    translations = data.get("translations", [])
                    for j, translated_item in enumerate(translations):
                        index_in_results = i + j
                        if index_in_results < len(result_texts):
                            result_texts[index_in_results] = translated_item.get("text", batch_texts[j])
                else:
                    logger.error(
                        "DeepL text-only API error (HTTP {}): {}",
                        response.status_code,
                        response.text[:300]
                    )
            except Exception as exc:
                logger.error("Network error during DeepL fallback (texts): {}", exc)

        return result_texts

    # ── Word-level recheck against ElevenLabs timestamps ──────────────────

    @staticmethod
    def recheck_word_level_alignment(
        translated_segments: list[TranscriptSegment],
        original_words: list[WordTimestamp],
        original_speakers: list[str],
    ) -> list[TranscriptSegment]:
        """Recheck translated segments against ElevenLabs word-level timestamps.

        After Gemini regrouping/translation, segment timing can drift from the
        actual ElevenLabs word-level data.  This method validates and corrects:

        1. **Snap segment boundaries** — force ``seg.start`` / ``seg.end`` to
           match the first/last word timestamps exactly.
        2. **Missing word recovery** — detect any ElevenLabs words not covered
           by any segment and insert them into the nearest segment or create
           new mini-segments.
        3. **Duplicate word removal** — if a word appears in more than one
           segment, keep it only in the chronologically correct one.
        4. **Word order validation** — sort words within each segment by
           ``start`` time.
        5. **Segment chronological order** — sort segments and prevent
           overlapping boundaries.
        6. **Gap detection** — log warnings about time regions where speech
           exists but no segment covers it.

        Parameters
        ----------
        translated_segments:
            Segments after Gemini translation/regrouping.
        original_words:
            All word-level timestamps from ElevenLabs (flattened, chronological).
        original_speakers:
            Speaker ID per word (same length as ``original_words``).

        Returns
        -------
        list[TranscriptSegment]
            Rechecked and fixed segments.
        """
        if not translated_segments or not original_words:
            return translated_segments

        # Build a lookup from (start, end) → original word/speaker
        # for fast matching.  Using rounded keys to tolerate float precision.
        orig_lookup: dict[tuple[float, float], tuple[int, WordTimestamp, str]] = {}
        for idx, (w, sp) in enumerate(zip(original_words, original_speakers)):
            key = (round(w.start, 4), round(w.end, 4))
            orig_lookup[key] = (idx, w, sp)

        # Build a set of original word indices already covered by segments
        covered_indices: set[int] = set()
        # Map: original word index → list of segment indices that contain it
        word_to_seg: dict[int, list[int]] = {}

        fixes_snap = 0
        fixes_order = 0
        fixes_dup = 0
        fixes_missing = 0
        fixes_gap = 0

        # ── Pass 1: Map segment words back to original ElevenLabs indices ──
        seg_word_indices: list[list[int]] = []  # per-segment list of original indices

        for seg_idx, seg in enumerate(translated_segments):
            indices_in_seg: list[int] = []
            for w in seg.words:
                key = (round(w.start, 4), round(w.end, 4))
                match = orig_lookup.get(key)
                if match:
                    orig_idx, orig_w, _ = match
                    indices_in_seg.append(orig_idx)
                    word_to_seg.setdefault(orig_idx, []).append(seg_idx)
                    covered_indices.add(orig_idx)
            seg_word_indices.append(indices_in_seg)

        # ── Pass 2: Remove duplicate word assignments ──────────────────────
        # A word should appear in exactly one segment.  Keep it in the segment
        # where its index is most chronologically consistent.
        for orig_idx, seg_list in word_to_seg.items():
            if len(seg_list) <= 1:
                continue

            # Keep the word in the segment whose other indices are closest
            best_seg = seg_list[0]
            best_fit = float("inf")
            for s_idx in seg_list:
                other_indices = [i for i in seg_word_indices[s_idx] if i != orig_idx]
                if other_indices:
                    # Fitness = average index distance to this word
                    fit = sum(abs(i - orig_idx) for i in other_indices) / len(other_indices)
                else:
                    fit = 0  # sole word in segment → keep it here
                if fit < best_fit:
                    best_fit = fit
                    best_seg = s_idx

            # Remove word from all other segments
            orig_w = original_words[orig_idx]
            for s_idx in seg_list:
                if s_idx == best_seg:
                    continue
                seg = translated_segments[s_idx]
                seg.words = [
                    w for w in seg.words
                    if not (round(w.start, 4) == round(orig_w.start, 4)
                            and round(w.end, 4) == round(orig_w.end, 4))
                ]
                fixes_dup += 1

            logger.debug(
                "recheck: duplicate word '{}' @{:.3f}s removed from {} segment(s), "
                "kept in seg#{}",
                orig_w.word, orig_w.start, len(seg_list) - 1, best_seg,
            )

        # ── Pass 3: Sort words within each segment by start time ───────────
        for seg in translated_segments:
            sorted_words = sorted(seg.words, key=lambda w: w.start)
            if [w.start for w in seg.words] != [w.start for w in sorted_words]:
                fixes_order += 1
            seg.words = sorted_words

        # ── Pass 4: Snap segment boundaries to exact word timestamps ───────
        for seg in translated_segments:
            if not seg.words:
                continue
            expected_start = round(seg.words[0].start, 3)
            expected_end = round(seg.words[-1].end, 3)
            if abs(seg.start - expected_start) > 0.001:
                logger.debug(
                    "recheck: snap seg start {:.3f} → {:.3f} (text: '{}')",
                    seg.start, expected_start, seg.text[:40],
                )
                seg.start = expected_start
                fixes_snap += 1
            if abs(seg.end - expected_end) > 0.001:
                logger.debug(
                    "recheck: snap seg end {:.3f} → {:.3f} (text: '{}')",
                    seg.end, expected_end, seg.text[:40],
                )
                seg.end = expected_end
                fixes_snap += 1

        # ── Pass 5: Recover missing words ──────────────────────────────────
        all_indices = set(range(len(original_words)))
        missing_indices = sorted(all_indices - covered_indices)

        if missing_indices:
            logger.warning(
                "recheck: {} ElevenLabs word(s) missing from translated segments — recovering",
                len(missing_indices),
            )

            # Group consecutive missing indices
            groups: list[list[int]] = []
            current_group: list[int] = [missing_indices[0]]
            for i in range(1, len(missing_indices)):
                if missing_indices[i] == missing_indices[i - 1] + 1:
                    current_group.append(missing_indices[i])
                else:
                    groups.append(current_group)
                    current_group = [missing_indices[i]]
            groups.append(current_group)

            for group_indices in groups:
                grp_words = [original_words[i] for i in group_indices]
                grp_speakers = [original_speakers[i] for i in group_indices]
                grp_start = grp_words[0].start
                grp_end = grp_words[-1].end
                grp_mid = (grp_start + grp_end) / 2

                # Try to insert into an adjacent segment if the gap is small
                inserted = False
                best_seg_idx = -1
                best_distance = float("inf")

                for seg_idx, seg in enumerate(translated_segments):
                    if not seg.words:
                        continue
                    seg_first = seg.words[0].start
                    seg_last = seg.words[-1].end

                    # Check: does this group fit right before the segment?
                    if grp_end <= seg_first:
                        distance = seg_first - grp_end
                        if distance < 0.5 and distance < best_distance:
                            # Also check speaker compatibility
                            majority_sp = max(set(grp_speakers), key=grp_speakers.count)
                            if seg.speaker == majority_sp:
                                best_distance = distance
                                best_seg_idx = seg_idx

                    # Check: does this group fit right after the segment?
                    if grp_start >= seg_last:
                        distance = grp_start - seg_last
                        if distance < 0.5 and distance < best_distance:
                            majority_sp = max(set(grp_speakers), key=grp_speakers.count)
                            if seg.speaker == majority_sp:
                                best_distance = distance
                                best_seg_idx = seg_idx

                if best_seg_idx >= 0:
                    # Insert into existing segment — add words for timing
                    # but do NOT modify seg.text (which is already translated).
                    # Appending source-language words to translated text causes
                    # mixed-language subtitles.
                    seg = translated_segments[best_seg_idx]
                    seg.words.extend(grp_words)
                    seg.words.sort(key=lambda w: w.start)
                    # Update segment boundaries
                    seg.start = round(seg.words[0].start, 3)
                    seg.end = round(seg.words[-1].end, 3)
                    fixes_missing += len(group_indices)
                    logger.debug(
                        "recheck: inserted {} missing word(s) into seg#{} "
                        "('{}')",
                        len(group_indices), best_seg_idx,
                        " ".join(w.word for w in grp_words),
                    )
                    inserted = True

                if not inserted:
                    # No adjacent segment with matching speaker — find the
                    # nearest segment by time distance and merge words there.
                    # Do NOT create standalone segments with untranslated text
                    # as this injects source-language text into the output.
                    best_any_idx = -1
                    best_any_dist = float("inf")
                    for seg_idx, seg in enumerate(translated_segments):
                        if not seg.words:
                            continue
                        seg_mid = (seg.words[0].start + seg.words[-1].end) / 2
                        dist = abs(grp_mid - seg_mid)
                        if dist < best_any_dist:
                            best_any_dist = dist
                            best_any_idx = seg_idx

                    if best_any_idx >= 0:
                        seg = translated_segments[best_any_idx]
                        seg.words.extend(grp_words)
                        seg.words.sort(key=lambda w: w.start)
                        seg.start = round(seg.words[0].start, 3)
                        seg.end = round(seg.words[-1].end, 3)
                        fixes_missing += len(group_indices)
                        logger.debug(
                            "recheck: merged {} missing word(s) into nearest seg#{} "
                            "@{:.3f}s-{:.3f}s ('{}')",
                            len(group_indices), best_any_idx,
                            grp_start, grp_end,
                            " ".join(w.word for w in grp_words),
                        )
                    else:
                        logger.warning(
                            "recheck: could not place {} missing word(s) "
                            "@{:.3f}s-{:.3f}s — no segments available",
                            len(group_indices), grp_start, grp_end,
                        )

        # ── Pass 6: Remove empty segments ──────────────────────────────────
        translated_segments = [seg for seg in translated_segments if seg.words]

        # ── Pass 7: Sort segments chronologically ──────────────────────────
        translated_segments.sort(key=lambda s: (s.start, s.end))

        # ── Pass 8: Detect and fix segment overlaps ────────────────────────
        for i in range(len(translated_segments) - 1):
            cur = translated_segments[i]
            nxt = translated_segments[i + 1]
            if cur.end > nxt.start:
                gap_fix = round(nxt.start - 0.001, 3)
                if gap_fix > cur.start:
                    logger.debug(
                        "recheck: trimming seg overlap: seg#{} end {:.3f} → {:.3f}",
                        i, cur.end, gap_fix,
                    )
                    cur.end = gap_fix
                    if cur.words and cur.words[-1].end > cur.end:
                        cur.words[-1].end = cur.end
                    fixes_gap += 1

        # ── Pass 9: Validate word timestamps against ElevenLabs source ─────
        # For each word in each segment, verify it matches an original
        # ElevenLabs word exactly.  If timestamps were modified (e.g. by a
        # previous sanitize pass), restore them from the ElevenLabs source.
        fixes_restore = 0
        for seg in translated_segments:
            for i, w in enumerate(seg.words):
                # Find the closest original word by start time
                best_match: WordTimestamp | None = None
                best_dist = float("inf")
                for ow in original_words:
                    dist = abs(w.start - ow.start)
                    if dist < best_dist:
                        best_dist = dist
                        best_match = ow
                    elif dist > best_dist + 1.0:
                        break  # original_words is sorted, no need to keep looking

                if best_match and best_dist < 0.1:
                    # Restore exact ElevenLabs timestamps
                    if (abs(w.start - best_match.start) > 0.001
                            or abs(w.end - best_match.end) > 0.001):
                        w.start = best_match.start
                        w.end = best_match.end
                        fixes_restore += 1

            # Re-snap segment boundaries after restoring word timestamps
            if seg.words:
                seg.start = round(seg.words[0].start, 3)
                seg.end = round(seg.words[-1].end, 3)

        # ── Summary ────────────────────────────────────────────────────────
        total_fixes = (
            fixes_snap + fixes_order + fixes_dup
            + fixes_missing + fixes_gap + fixes_restore
        )
        if total_fixes > 0:
            logger.info(
                "recheck_word_level_alignment: {} total fix(es) — "
                "snap={}, order={}, dup={}, missing={}, overlap={}, "
                "restore={}",
                total_fixes, fixes_snap, fixes_order, fixes_dup,
                fixes_missing, fixes_gap, fixes_restore,
            )
        else:
            logger.info(
                "recheck_word_level_alignment: all {} segments OK — "
                "no fixes needed ({}  words verified)",
                len(translated_segments), len(original_words),
            )

        return translated_segments

    # ── Word-level regrouping + translation ─────────────────────────────────

    async def _translate_and_regroup_gemini(
        self,
        segments: list[TranscriptSegment],
        api_keys: list[str],
    ) -> list[TranscriptSegment]:
        """Extract word-level data, send to Gemini for subtitle grouping + translation."""
        lang_name = LANGUAGE_NAMES.get(self.target_language, self.target_language)

        # Flatten all words with speaker info
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
            return await self._translate_batch_gemini(segments, api_keys)

        # Split into batches at natural break points
        batches = self._build_word_batches(all_words, word_speakers)

        all_new_segments: list[TranscriptSegment] = []
        for batch_idx, (batch_words, batch_speakers) in enumerate(batches):
            logger.info(
                "Regrouping + translating batch {}/{} ({} words) to '{}'...",
                batch_idx + 1,
                len(batches),
                len(batch_words),
                lang_name,
            )
            new_segs = await self._call_gemini_regroup(
                batch_words, batch_speakers, lang_name, api_keys
            )
            all_new_segments.extend(new_segs)

        if not all_new_segments:
            logger.warning("Gemini regrouping produced no segments — using fallback")
            return self._local_group_from_segments(segments)

        logger.info(
            "Regrouping complete: {} words → {} subtitle segments",
            len(all_words),
            len(all_new_segments),
        )
        return all_new_segments

    @staticmethod
    def _build_word_batches(
        all_words: list[WordTimestamp],
        speakers: list[str],
        max_words: int = 150,
    ) -> list[tuple[list[WordTimestamp], list[str]]]:
        """Split word list into batches at natural break points.

        Tries to split at speaker changes or long pauses (>1 s) to keep
        each batch contextually coherent for Gemini.
        """
        if len(all_words) <= max_words:
            return [(all_words, speakers)]

        batches: list[tuple[list[WordTimestamp], list[str]]] = []
        batch_start = 0
        i = 1

        while i < len(all_words):
            batch_size = i - batch_start

            if batch_size >= max_words:
                # Look backward for a good break point
                best_break = i
                for j in range(i, max(batch_start + 1, i - 50), -1):
                    gap = all_words[j].start - all_words[j - 1].end
                    if speakers[j] != speakers[j - 1] or gap > 1.0:
                        best_break = j
                        break

                batches.append((
                    all_words[batch_start:best_break],
                    speakers[batch_start:best_break],
                ))
                batch_start = best_break

            i += 1

        # Last batch
        if batch_start < len(all_words):
            batches.append((
                all_words[batch_start:],
                speakers[batch_start:],
            ))

        return batches

    @staticmethod
    def _repair_truncated_json(raw_text: str) -> list[dict] | None:
        """Attempt to salvage complete groups from truncated Gemini JSON.

        When the Gemini response exceeds maxOutputTokens, the JSON array gets
        cut off mid-object.  This function extracts all *complete* group objects
        from the truncated response so we don't lose the work already done.

        Returns a list of group dicts, or None if nothing could be salvaged.
        """
        # Try to find all complete {"indices": [...], "translated": "..."} objects
        # using a regex that matches balanced JSON objects
        pattern = re.compile(
            r'\{\s*"indices"\s*:\s*\[([\d\s,]+)\]\s*,\s*"translated"\s*:\s*"((?:[^"\\]|\\.)*)"\s*\}',
            re.DOTALL,
        )
        matches = list(pattern.finditer(raw_text))
        if not matches:
            return None

        groups: list[dict] = []
        for m in matches:
            try:
                indices_str = m.group(1).strip()
                indices = [int(x.strip()) for x in indices_str.split(",") if x.strip()]
                translated = m.group(2)
                # Unescape JSON string escapes
                translated = translated.replace('\\"', '"').replace('\\n', '\n').replace('\\\\', '\\')
                if indices and translated.strip():
                    groups.append({"indices": indices, "translated": translated})
            except (ValueError, IndexError):
                continue

        if groups:
            logger.info(
                "Salvaged {} complete group(s) from truncated Gemini JSON",
                len(groups),
            )
            return groups
        return None

    async def _call_gemini_regroup(
        self,
        words: list[WordTimestamp],
        speakers: list[str],
        target_lang_name: str,
        api_keys: list[str],
    ) -> list[TranscriptSegment]:
        """Call Gemini to group words into subtitle segments and translate.

        Falls back to local heuristic grouping if all API keys fail.
        """
        # Build compact word list with pause and speaker-change indicators
        lines: list[str] = []
        multi_speaker = len(set(speakers)) > 1

        for i, (w, sp) in enumerate(zip(words, speakers)):
            # Pause indicator
            if i > 0:
                gap = w.start - words[i - 1].end
                if gap > 0.7:
                    lines.append(f"[PAUSE {gap:.1f}s]")

            parts = [f"{i}. {w.word}"]

            # Speaker tag (only when there are multiple speakers)
            if multi_speaker:
                if i == 0 or speakers[i] != speakers[i - 1]:
                    parts.append(f"[{sp}]")

            lines.append(" ".join(parts))

        word_list_text = "\n".join(lines)

        prompt = (
            f"You are a subtitle segmentation and translation assistant.\n\n"
            f"Below is a numbered list of transcribed words from a video.\n"
            f"[PAUSE Xs] markers indicate silence gaps between words.\n"
        )
        if multi_speaker:
            prompt += f"[SPEAKER_XX] tags indicate speaker changes.\n"
        prompt += (
            f"\nYour tasks:\n"
            f"1. GROUP these words into natural subtitle segments:\n"
            f"   - Maximum 12 words per subtitle\n"
            f"   - Break at sentence boundaries (., !, ?) when possible\n"
            f"   - ALWAYS start a new subtitle at [PAUSE] markers\n"
        )
        if multi_speaker:
            prompt += f"   - ALWAYS start a new subtitle on speaker changes\n"
        prompt += (
            f"   - Keep each subtitle as a complete phrase or sentence\n"
            f"   - NEVER create a group whose translation is only punctuation "
            f"(e.g. '.', '!', '?'). Always include at least one meaningful word\n"
            f"2. TRANSLATE each subtitle group into {target_lang_name}:\n"
            f"   - Preserve emotional elongation "
            f"(e.g. 'noooooo' → equivalent stretched form in target language)\n"
            f"   - Keep expressive vocalizations EXACTLY as-is: onomatopoeia, "
            f"exclamations, romaji sounds ('kyaaaa', 'uwaaaa'), "
            f"universal vocal sounds ('ahhhh', 'ohhhh', 'ahahaha')\n"
            f"   - The translated text must be a proper subtitle line, never just "
            f"punctuation or a single symbol\n"
            f"3. FIX any broken/fragmented words:\n"
            f"   - If consecutive words look like fragments of one word "
            f"(e.g. 'beau' + 'tiful' = 'beautiful', "
            f"'un' + 'fortunately' = 'unfortunately'), "
            f"treat them as a single word in your translation.\n\n"
            f"Words:\n{word_list_text}\n\n"
            f"Return ONLY a strictly valid JSON array where each element is:\n"
            f'{{"indices": [0, 1, 2], "translated": "translated subtitle text"}}\n\n'
            f"CRITICAL RULES:\n"
            f"- Keep your response concise — use short, natural subtitle translations\n"
            f"- ALWAYS escape double quotes inside the translation text using a backslash (e.g. \\\"Hello\\\")\n"
            f"- Do NOT leave trailing commas in JSON arrays or objects\n"
            f"- You MUST include EVERY word index from 0 to {len(words) - 1} — "
            f"do NOT skip any index\n"
            f"- Every word index must appear in exactly one group\n"
            f"- Groups must be in chronological order\n"
            f"- Indices within each group must be consecutive\n"
            f"- The total number of indices across all groups must equal {len(words)}\n"
            f"- If the first word is index 0 and the last is index {len(words) - 1}, "
            f"then indices 0, 1, 2, ..., {len(words) - 1} must all be present\n"
        )

        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": 32768,
                "responseMimeType": "application/json",
            },
        }

        last_error = None
        best_partial_groups: list[dict] | None = None  # salvaged from truncated responses

        for key_idx, api_key in enumerate(api_keys):
            try:
                url = f"{GEMINI_API_URL}?key={api_key}"
                async with httpx.AsyncClient(timeout=180.0) as client:
                    response = await client.post(url, json=payload)

                if response.status_code in (429, 403):
                    logger.warning(
                        "Gemini Key #{} rate-limited (HTTP {}), trying next...",
                        key_idx + 1,
                        response.status_code,
                    )
                    last_error = f"HTTP {response.status_code}"
                    continue

                if response.status_code != 200:
                    logger.error(
                        "Gemini regroup API error (HTTP {}): {}",
                        response.status_code,
                        response.text[:300],
                    )
                    last_error = f"HTTP {response.status_code}"
                    continue

                result = response.json()
                candidates = result.get("candidates", [])
                if not candidates:
                    logger.warning("Gemini returned no candidates for regrouping")
                    last_error = "No candidates"
                    continue

                # ── Check for output truncation via finishReason ──────────
                finish_reason = candidates[0].get("finishReason", "")
                is_truncated = finish_reason in ("MAX_TOKENS", "RECITATION")

                content = candidates[0].get("content", {})
                parts = content.get("parts", [])
                raw_text = parts[0].get("text", "") if parts else ""

                # Clean up the raw text to handle common Gemini JSON formatting issues
                raw_text = raw_text.strip()
                if raw_text.startswith("```"):
                    raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text)
                    raw_text = re.sub(r"\s*```$", "", raw_text)
                raw_text = re.sub(r",(\s*[\]}])", r"\1", raw_text)

                if is_truncated:
                    logger.warning(
                        "Gemini Key #{} output truncated (finishReason={}). "
                        "Attempting to salvage partial JSON...",
                        key_idx + 1,
                        finish_reason,
                    )
                    salvaged = self._repair_truncated_json(raw_text)
                    if salvaged:
                        # Keep the best (most groups) partial result across retries
                        if not best_partial_groups or len(salvaged) > len(best_partial_groups):
                            best_partial_groups = salvaged
                    last_error = f"Output truncated ({finish_reason})"
                    continue

                try:
                    groups = json.loads(raw_text)
                except json.JSONDecodeError as parse_exc:
                    # JSON parse failed — try to salvage complete groups
                    logger.warning(
                        "Failed to parse Gemini regroup JSON: {} (Snippet: {})",
                        parse_exc,
                        raw_text[max(0, parse_exc.pos - 50):parse_exc.pos + 50]
                        if hasattr(parse_exc, "pos") else raw_text[:200],
                    )
                    salvaged = self._repair_truncated_json(raw_text)
                    if salvaged:
                        if not best_partial_groups or len(salvaged) > len(best_partial_groups):
                            best_partial_groups = salvaged
                    last_error = f"JSON parse: {parse_exc}"
                    continue

                if not isinstance(groups, list) or not groups:
                    logger.warning(
                        "Unexpected Gemini regroup response: {}",
                        raw_text[:200],
                    )
                    last_error = "Unexpected format"
                    continue

                segments = self._reconstruct_segments(groups, words, speakers)
                if segments:
                    return segments

                last_error = "Segment reconstruction failed"

            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                logger.warning(
                    "Gemini Key #{} network error: {}", key_idx + 1, exc
                )
                last_error = str(exc)
                continue

        # ── All keys exhausted: try to use partial results if available ────
        if best_partial_groups:
            logger.info(
                "Using {} salvaged group(s) from truncated Gemini response + "
                "local fallback for remaining words",
                len(best_partial_groups),
            )
            # Reconstruct segments from salvaged groups
            partial_segs = self._reconstruct_segments(
                best_partial_groups, words, speakers
            )
            if partial_segs:
                # Find which word indices were covered
                covered_indices: set[int] = set()
                for g in best_partial_groups:
                    for idx in g.get("indices", []):
                        if 0 <= idx < len(words):
                            covered_indices.add(idx)

                # Build local segments for uncovered words
                uncovered = sorted(set(range(len(words))) - covered_indices)
                if uncovered:
                    logger.info(
                        "Locally grouping {} uncovered word(s) and translating with DeepL",
                        len(uncovered),
                    )
                    # Group consecutive uncovered indices
                    unc_words: list[WordTimestamp] = []
                    unc_speakers: list[str] = []
                    for idx in uncovered:
                        unc_words.append(words[idx])
                        unc_speakers.append(speakers[idx])

                    local_segs = self._local_group_words(unc_words, unc_speakers)
                    translated_local = await self._translate_fallback_deepl(local_segs)
                    partial_segs.extend(translated_local)
                    partial_segs.sort(key=lambda s: s.start)

                return partial_segs

        logger.error(
            "All Gemini API keys failed for regrouping. Last error: {}. "
            "Using local fallback with DeepL.",
            last_error,
        )
        local_segs = self._local_group_words(words, speakers)
        return await self._translate_fallback_deepl(local_segs)

    async def _translate_fallback_deepl(
        self,
        segments: list[TranscriptSegment]
    ) -> list[TranscriptSegment]:
        """Translate segments using DeepL API as a fallback when Gemini fails."""
        if not segments:
            return segments

        if not config.DEEPL_API_KEY:
            logger.warning(
                "DeepL fallback skipped: DEEPL_API_KEY not configured. "
                "Segments will be returned with their source-language text."
            )
            return segments

        logger.info("Starting DeepL fallback translation for {} segments...", len(segments))

        target_code = self.target_language.upper()
        if target_code == "EN":
            target_code = "EN-US"
        elif target_code == "PT":
            target_code = "PT-PT"

        url = "https://api-free.deepl.com/v2/translate"
        headers = {
            "Authorization": f"DeepL-Auth-Key {config.DEEPL_API_KEY}",
            "Content-Type": "application/json"
        }

        # Translating in batches to comply with DeepL's array size limit (max 50)
        batch_size = 50
        import httpx

        for i in range(0, len(segments), batch_size):
            batch = segments[i:i + batch_size]
            texts_to_translate = [seg.text for seg in batch]

            payload = {
                "text": texts_to_translate,
                "target_lang": target_code
            }

            try:
                async with httpx.AsyncClient(timeout=60.0) as client:
                    response = await client.post(url, headers=headers, json=payload)
                
                if response.status_code == 200:
                    data = response.json()
                    translations = data.get("translations", [])
                    for j, translated_item in enumerate(translations):
                        # Update the segment text with DeepL's translation
                        if j < len(batch):
                            batch[j].text = translated_item.get("text", batch[j].text)
                else:
                    logger.error(
                        "DeepL API error (HTTP {}): {}",
                        response.status_code,
                        response.text[:300]
                    )
            except Exception as exc:
                logger.error("Network error during DeepL fallback: {}", exc)

        return segments

    @staticmethod
    def _reconstruct_segments(
        groups: list[dict],
        words: list[WordTimestamp],
        speakers: list[str],
    ) -> list[TranscriptSegment]:
        """Build TranscriptSegment objects from Gemini's grouping response."""
        segments: list[TranscriptSegment] = []
        used_indices: set[int] = set()

        for group in groups:
            indices = group.get("indices", [])
            translated = str(group.get("translated", "")).strip()

            if not indices or not translated:
                continue

            # Skip groups where the translated text is only punctuation/symbols
            if re.fullmatch(r'[\s\.\!\?\,\;\:\-\—\–\…\"\'«»""'']+', translated):
                logger.debug(
                    "Skipping punctuation-only group: indices={}, text='{}'",
                    indices, translated,
                )
                # Still mark indices as used so they get merged into
                # adjacent segments rather than becoming "missing"
                valid_skip = [i for i in indices if 0 <= i < len(words)]
                used_indices.update(valid_skip)
                # The words from this group need to be absorbed by neighbors;
                # they will be handled by the missing-word recovery below
                # if no neighbor claims them. But let's proactively merge
                # them here: find the last segment we created and extend it.
                if segments and valid_skip:
                    skip_words = [words[i] for i in valid_skip]
                    segments[-1].words.extend(skip_words)
                    segments[-1].words.sort(key=lambda w: w.start)
                    segments[-1].end = round(segments[-1].words[-1].end, 3)
                continue

            # Validate indices
            valid = [
                i for i in indices
                if 0 <= i < len(words) and i not in used_indices
            ]
            if not valid:
                continue

            used_indices.update(valid)

            group_words = [words[i] for i in valid]
            group_speakers = [speakers[i] for i in valid]
            speaker = max(set(group_speakers), key=group_speakers.count)

            segments.append(
                TranscriptSegment(
                    start=round(group_words[0].start, 3),
                    end=round(group_words[-1].end, 3),
                    text=translated,
                    speaker=speaker,
                    words=group_words,
                )
            )

        # Handle any unassigned words — merge into nearest segment
        # instead of creating standalone segments with untranslated text.
        # Creating separate segments would inject source-language text into
        # the subtitle output.
        missing = sorted(set(range(len(words))) - used_indices)
        if missing:
            logger.warning(
                "Gemini regrouping missed {} word(s) — merging into nearest segments",
                len(missing),
            )
            # Group consecutive missing indices
            group_start = 0
            for k in range(1, len(missing) + 1):
                if k == len(missing) or missing[k] != missing[k - 1] + 1:
                    idx_range = missing[group_start:k]
                    grp_words = [words[i] for i in idx_range]
                    grp_mid = (grp_words[0].start + grp_words[-1].end) / 2

                    # Find the nearest existing segment to absorb these words
                    best_seg_idx = -1
                    best_dist = float("inf")
                    for si, seg in enumerate(segments):
                        # Distance from group midpoint to segment span
                        seg_mid = (seg.start + seg.end) / 2
                        dist = abs(grp_mid - seg_mid)
                        if dist < best_dist:
                            best_dist = dist
                            best_seg_idx = si

                    if best_seg_idx >= 0:
                        # Merge words into nearest segment (for timing) but
                        # do NOT modify the segment's translated text.
                        seg = segments[best_seg_idx]
                        seg.words.extend(grp_words)
                        seg.words.sort(key=lambda w: w.start)
                        seg.start = round(seg.words[0].start, 3)
                        seg.end = round(seg.words[-1].end, 3)
                        logger.debug(
                            "Merged {} missed word(s) into seg#{} (words: '{}')",
                            len(idx_range), best_seg_idx,
                            " ".join(w.word for w in grp_words),
                        )
                    else:
                        # No segments to merge into — this shouldn't happen
                        # but log it for debugging.
                        logger.warning(
                            "No segment to merge {} missed word(s) into: '{}'",
                            len(idx_range),
                            " ".join(w.word for w in grp_words),
                        )
                    group_start = k

        segments.sort(key=lambda s: s.start)
        return segments

    @staticmethod
    def _local_group_words(
        words: list[WordTimestamp],
        speakers: list[str],
    ) -> list[TranscriptSegment]:
        """Fallback: group words using pause/speaker/sentence heuristics (no Gemini)."""
        if not words:
            return []

        segments: list[TranscriptSegment] = []
        cur: list[WordTimestamp] = []
        cur_sp = speakers[0]

        for i, (w, sp) in enumerate(zip(words, speakers)):
            flush = False
            if cur:
                if sp != cur_sp:
                    flush = True
                elif w.start - cur[-1].end > 0.7:
                    flush = True
                elif len(cur) >= 12:
                    flush = True
                elif cur[-1].word.rstrip()[-1:] in ".!?" and len(cur) >= 3:
                    flush = True

            if flush:
                segments.append(
                    TranscriptSegment(
                        start=round(cur[0].start, 3),
                        end=round(cur[-1].end, 3),
                        text=" ".join(cw.word for cw in cur),
                        speaker=cur_sp,
                        words=list(cur),
                    )
                )
                cur = []

            cur.append(w)
            cur_sp = sp

        if cur:
            segments.append(
                TranscriptSegment(
                    start=round(cur[0].start, 3),
                    end=round(cur[-1].end, 3),
                    text=" ".join(cw.word for cw in cur),
                    speaker=cur_sp,
                    words=list(cur),
                )
            )

        return segments

    @staticmethod
    def _local_group_from_segments(
        segments: list[TranscriptSegment],
    ) -> list[TranscriptSegment]:
        """Flatten segments to word-level and regroup with local heuristics."""
        all_words: list[WordTimestamp] = []
        speakers: list[str] = []
        for seg in segments:
            for w in seg.words:
                all_words.append(w)
                speakers.append(seg.speaker)

        if not all_words:
            return segments

        return TranslatorProcessor._local_group_words(all_words, speakers)

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
