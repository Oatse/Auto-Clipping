"""
processors.translator.recheck — Word-level alignment recheck.

After Gemini regrouping or any other text edit, segment timing can
drift from the actual ElevenLabs word-level data.  This module
validates and corrects that drift in 9 passes (snap, sort, dedup,
recover missing, fix overlap, restore exact ts).

Single public entrypoint: :func:`recheck_word_level_alignment`.
"""

from __future__ import annotations

from loguru import logger

from models.transcript import TranscriptSegment, WordTimestamp


def recheck_word_level_alignment(
    translated_segments: list[TranscriptSegment],
    original_words: list[WordTimestamp],
    original_speakers: list[str],
) -> list[TranscriptSegment]:
    """Recheck translated segments against ElevenLabs word-level timestamps.

    After Gemini regrouping/translation, segment timing can drift from the
    actual ElevenLabs word-level data.  This function validates and corrects:

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
    7. **Restore exact timestamps** — for each word, snap timing back to
       the closest ElevenLabs source word (within 100 ms).

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

    covered_indices: set[int] = set()
    word_to_seg: dict[int, list[int]] = {}

    fixes_snap = 0
    fixes_order = 0
    fixes_dup = 0
    fixes_missing = 0
    fixes_gap = 0

    # ── Pass 1: Map segment words back to original ElevenLabs indices ──
    seg_word_indices: list[list[int]] = []

    for seg_idx, seg in enumerate(translated_segments):
        indices_in_seg: list[int] = []
        for w in seg.words:
            key = (round(w.start, 4), round(w.end, 4))
            match = orig_lookup.get(key)
            if match:
                orig_idx, _, _ = match
                indices_in_seg.append(orig_idx)
                word_to_seg.setdefault(orig_idx, []).append(seg_idx)
                covered_indices.add(orig_idx)
        seg_word_indices.append(indices_in_seg)

    # ── Pass 2: Remove duplicate word assignments ──
    for orig_idx, seg_list in word_to_seg.items():
        if len(seg_list) <= 1:
            continue

        best_seg = seg_list[0]
        best_fit = float("inf")
        for s_idx in seg_list:
            other_indices = [i for i in seg_word_indices[s_idx] if i != orig_idx]
            if other_indices:
                fit = sum(abs(i - orig_idx) for i in other_indices) / len(other_indices)
            else:
                fit = 0
            if fit < best_fit:
                best_fit = fit
                best_seg = s_idx

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

    # ── Pass 3: Sort words within each segment by start time ──
    for seg in translated_segments:
        sorted_words = sorted(seg.words, key=lambda w: w.start)
        if [w.start for w in seg.words] != [w.start for w in sorted_words]:
            fixes_order += 1
        seg.words = sorted_words

    # ── Pass 4: Snap segment boundaries to exact word timestamps ──
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

    # ── Pass 5: Recover missing words ──
    fixes_missing = _recover_missing_words(
        translated_segments,
        original_words,
        original_speakers,
        covered_indices,
    )

    # ── Pass 6: Remove empty segments ──
    translated_segments = [seg for seg in translated_segments if seg.words]

    # ── Pass 7: Sort segments chronologically ──
    translated_segments.sort(key=lambda s: (s.start, s.end))

    # ── Pass 8: Detect and fix segment overlaps ──
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

    # ── Pass 9: Validate word timestamps against ElevenLabs source ──
    fixes_restore = _restore_exact_timestamps(translated_segments, original_words)

    # ── Summary ──
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


def _recover_missing_words(
    translated_segments: list[TranscriptSegment],
    original_words: list[WordTimestamp],
    original_speakers: list[str],
    covered_indices: set[int],
) -> int:
    """Insert ElevenLabs words that were dropped by Gemini.

    Returns the number of words recovered.
    """
    all_indices = set(range(len(original_words)))
    missing_indices = sorted(all_indices - covered_indices)
    if not missing_indices:
        return 0

    logger.warning(
        "recheck: {} ElevenLabs word(s) missing from translated segments — recovering",
        len(missing_indices),
    )

    # Group consecutive missing indices.
    groups: list[list[int]] = []
    current_group: list[int] = [missing_indices[0]]
    for i in range(1, len(missing_indices)):
        if missing_indices[i] == missing_indices[i - 1] + 1:
            current_group.append(missing_indices[i])
        else:
            groups.append(current_group)
            current_group = [missing_indices[i]]
    groups.append(current_group)

    recovered = 0
    for group_indices in groups:
        grp_words = [original_words[i] for i in group_indices]
        grp_speakers = [original_speakers[i] for i in group_indices]
        grp_start = grp_words[0].start
        grp_end = grp_words[-1].end
        grp_mid = (grp_start + grp_end) / 2

        # Try to insert into an adjacent segment if the gap is small.
        inserted = False
        best_seg_idx = -1
        best_distance = float("inf")

        for seg_idx, seg in enumerate(translated_segments):
            if not seg.words:
                continue
            seg_first = seg.words[0].start
            seg_last = seg.words[-1].end

            # Group fits right before the segment?
            if grp_end <= seg_first:
                distance = seg_first - grp_end
                if distance < 0.5 and distance < best_distance:
                    majority_sp = max(set(grp_speakers), key=grp_speakers.count)
                    if seg.speaker == majority_sp:
                        best_distance = distance
                        best_seg_idx = seg_idx

            # Group fits right after the segment?
            if grp_start >= seg_last:
                distance = grp_start - seg_last
                if distance < 0.5 and distance < best_distance:
                    majority_sp = max(set(grp_speakers), key=grp_speakers.count)
                    if seg.speaker == majority_sp:
                        best_distance = distance
                        best_seg_idx = seg_idx

        if best_seg_idx >= 0:
            seg = translated_segments[best_seg_idx]
            seg.words.extend(grp_words)
            seg.words.sort(key=lambda w: w.start)
            seg.start = round(seg.words[0].start, 3)
            seg.end = round(seg.words[-1].end, 3)
            recovered += len(group_indices)
            logger.debug(
                "recheck: inserted {} missing word(s) into seg#{} ('{}')",
                len(group_indices), best_seg_idx,
                " ".join(w.word for w in grp_words),
            )
            inserted = True

        if not inserted:
            # No adjacent segment with matching speaker — find the
            # nearest segment by time distance and merge words there.
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
                recovered += len(group_indices)
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

    return recovered


def _restore_exact_timestamps(
    translated_segments: list[TranscriptSegment],
    original_words: list[WordTimestamp],
) -> int:
    """For each segment word, snap to nearest ElevenLabs word within 100 ms.

    Returns the number of timestamps restored.

    Optimisation: ``original_words`` is sorted chronologically, so the
    nearest neighbour by ``start`` time can be located via binary search
    in O(log N) per word instead of the previous O(N) linear scan.
    """
    import bisect

    if not original_words:
        return 0

    # Sort once and capture (start, original_word) pairs so the index
    # ordering matches the sorted starts list.
    sorted_originals = sorted(original_words, key=lambda w: w.start)
    sorted_starts = [w.start for w in sorted_originals]

    fixes = 0
    for seg in translated_segments:
        for w in seg.words:
            # Bisect to find the insertion point, then compare neighbours
            # at idx-1 and idx for the closer match.
            insert = bisect.bisect_left(sorted_starts, w.start)
            best_match: WordTimestamp | None = None
            best_dist = float("inf")
            for cand_idx in (insert - 1, insert):
                if 0 <= cand_idx < len(sorted_originals):
                    ow = sorted_originals[cand_idx]
                    dist = abs(w.start - ow.start)
                    if dist < best_dist:
                        best_dist = dist
                        best_match = ow

            if best_match and best_dist < 0.1:
                if (abs(w.start - best_match.start) > 0.001
                        or abs(w.end - best_match.end) > 0.001):
                    w.start = best_match.start
                    w.end = best_match.end
                    fixes += 1

        # Re-snap segment boundaries after restoring word timestamps.
        if seg.words:
            seg.start = round(seg.words[0].start, 3)
            seg.end = round(seg.words[-1].end, 3)
    return fixes
