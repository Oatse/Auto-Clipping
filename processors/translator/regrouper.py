"""
processors.translator.regrouper — Build subtitle segments from Gemini's
grouping response.

Two helpers:

* :func:`build_word_batches` — split a long word stream into manageable
  batches at natural break points (speaker change / long pause) so each
  Gemini call stays under the model's output token budget.

* :func:`reconstruct_segments` — turn the JSON ``[{"indices": [...],
  "translated": "..."}, ...]`` payload that Gemini returns into a list of
  :class:`TranscriptSegment`, while merging missed words into the nearest
  segment so source-language fragments don't leak through.
"""

from __future__ import annotations

import re

from loguru import logger

from models.transcript import TranscriptSegment, WordTimestamp

from .constants import PUNCTUATION_ONLY_PATTERN


def build_word_batches(
    all_words: list[WordTimestamp],
    speakers: list[str],
    max_words: int = 150,
) -> list[tuple[list[WordTimestamp], list[str]]]:
    """Split a flat word list into batches at natural break points.

    Tries to split at speaker changes or long pauses (>1 s) to keep each
    batch contextually coherent for Gemini.
    """
    if len(all_words) <= max_words:
        return [(all_words, speakers)]

    batches: list[tuple[list[WordTimestamp], list[str]]] = []
    batch_start = 0
    i = 1

    while i < len(all_words):
        batch_size = i - batch_start

        if batch_size >= max_words:
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

    if batch_start < len(all_words):
        batches.append((
            all_words[batch_start:],
            speakers[batch_start:],
        ))

    return batches


def reconstruct_segments(
    groups: list[dict],
    words: list[WordTimestamp],
    speakers: list[str],
) -> list[TranscriptSegment]:
    """Build :class:`TranscriptSegment` objects from Gemini's grouping response."""
    segments: list[TranscriptSegment] = []
    used_indices: set[int] = set()

    for group in groups:
        indices = group.get("indices", [])
        translated = str(group.get("translated", "")).strip()

        if not indices or not translated:
            continue

        # Skip groups whose translated text is only punctuation/symbols.
        if re.fullmatch(PUNCTUATION_ONLY_PATTERN, translated):
            logger.debug(
                "Skipping punctuation-only group: indices={}, text='{}'",
                indices, translated,
            )
            valid_skip = [i for i in indices if 0 <= i < len(words)]
            used_indices.update(valid_skip)
            # Proactively merge skipped words into the previous segment so
            # they aren't reported as missing later.
            if segments and valid_skip:
                skip_words = [words[i] for i in valid_skip]
                segments[-1].words.extend(skip_words)
                segments[-1].words.sort(key=lambda w: w.start)
                segments[-1].end = round(segments[-1].words[-1].end, 3)
            continue

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

    _merge_unassigned_words(segments, words, used_indices)
    segments.sort(key=lambda s: s.start)
    return segments


def _merge_unassigned_words(
    segments: list[TranscriptSegment],
    words: list[WordTimestamp],
    used_indices: set[int],
) -> None:
    """Merge any word indices Gemini missed into the nearest existing segment.

    Mutates ``segments`` in place.  Standalone fragment segments would
    inject source-language text into the otherwise translated output, so
    we always prefer absorbing into an existing segment.
    """
    missing = sorted(set(range(len(words))) - used_indices)
    if not missing:
        return

    logger.warning(
        "Gemini regrouping missed {} word(s) — merging into nearest segments",
        len(missing),
    )

    # Walk consecutive missing indices and absorb each run into the nearest
    # existing segment (by midpoint distance).
    group_start = 0
    for k in range(1, len(missing) + 1):
        if k == len(missing) or missing[k] != missing[k - 1] + 1:
            idx_range = missing[group_start:k]
            grp_words = [words[i] for i in idx_range]
            grp_mid = (grp_words[0].start + grp_words[-1].end) / 2

            best_seg_idx = -1
            best_dist = float("inf")
            for si, seg in enumerate(segments):
                seg_mid = (seg.start + seg.end) / 2
                dist = abs(grp_mid - seg_mid)
                if dist < best_dist:
                    best_dist = dist
                    best_seg_idx = si

            if best_seg_idx >= 0:
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
                logger.warning(
                    "No segment to merge {} missed word(s) into: '{}'",
                    len(idx_range),
                    " ".join(w.word for w in grp_words),
                )
            group_start = k
