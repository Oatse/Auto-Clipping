"""
processors/translator.py — Phase 2: Translation with Duration Awareness.

This module provides translation via Gemini API that:
  - Preserves original start/end/speaker timestamps.
  - Replaces only the text field with the translated version.
  - Saves output to translated_transcript.json.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
from loguru import logger

import config
from models.transcript import TranscriptSegment, WordTimestamp
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
    "gemini-2.5-flash:generateContent"
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
            "Returning original text.",
            last_error,
        )
        return texts

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
        max_words: int = 300,
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
            f"2. TRANSLATE each subtitle group into {target_lang_name}:\n"
            f"   - Preserve emotional elongation "
            f"(e.g. 'noooooo' → equivalent stretched form in target language)\n"
            f"   - Keep expressive vocalizations EXACTLY as-is: onomatopoeia, "
            f"exclamations, romaji sounds ('kyaaaa', 'uwaaaa'), "
            f"universal vocal sounds ('ahhhh', 'ohhhh', 'ahahaha')\n"
            f"3. FIX any broken/fragmented words:\n"
            f"   - If consecutive words look like fragments of one word "
            f"(e.g. 'beau' + 'tiful' = 'beautiful', "
            f"'un' + 'fortunately' = 'unfortunately'), "
            f"treat them as a single word in your translation.\n\n"
            f"Words:\n{word_list_text}\n\n"
            f"Return ONLY a JSON array where each element is:\n"
            f'{{"indices": [0, 1, 2], "translated": "translated subtitle text"}}\n\n'
            f"Rules:\n"
            f"- Every word index must appear in exactly one group\n"
            f"- Groups must be in chronological order\n"
            f"- Indices within each group must be consecutive\n"
            f"- Do NOT skip any word index\n"
        )

        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": 16384,
                "responseMimeType": "application/json",
            },
        }

        last_error = None
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

                content = candidates[0].get("content", {})
                parts = content.get("parts", [])
                raw_text = parts[0].get("text", "") if parts else ""

                groups = json.loads(raw_text)
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

            except json.JSONDecodeError as exc:
                logger.warning("Failed to parse Gemini regroup JSON: {}", exc)
                last_error = f"JSON parse: {exc}"
                continue

            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                logger.warning(
                    "Gemini Key #{} network error: {}", key_idx + 1, exc
                )
                last_error = str(exc)
                continue

        logger.error(
            "All Gemini API keys failed for regrouping. Last error: {}. "
            "Using local fallback.",
            last_error,
        )
        return self._local_group_words(words, speakers)

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

        # Handle any unassigned words
        missing = sorted(set(range(len(words))) - used_indices)
        if missing:
            logger.warning(
                "Gemini regrouping missed {} word(s) — adding as extra segments",
                len(missing),
            )
            # Group consecutive missing indices
            group_start = 0
            for k in range(1, len(missing) + 1):
                if k == len(missing) or missing[k] != missing[k - 1] + 1:
                    idx_range = missing[group_start:k]
                    grp_words = [words[i] for i in idx_range]
                    grp_speakers = [speakers[i] for i in idx_range]
                    sp = max(set(grp_speakers), key=grp_speakers.count)
                    text = " ".join(w.word for w in grp_words)
                    segments.append(
                        TranscriptSegment(
                            start=round(grp_words[0].start, 3),
                            end=round(grp_words[-1].end, 3),
                            text=text,
                            speaker=sp,
                            words=grp_words,
                        )
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
