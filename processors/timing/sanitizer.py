"""
processors.timing.sanitizer — The single canonical sanitizer.

Encapsulates every word- and segment-level timing fix in one class so
the same passes apply uniformly regardless of caller (STT pipeline,
translator regroup, preview re-sync, render).

Passes (full sanitize):

    0. sort segments chronologically
    1. estimate global speech-rate factor (loosen cap on fast/elongated speech)
    2. cap broken word durations
    3. fix same-speaker word overlap  (cross-speaker overlap preserved)
    4. recalc segment.start/end from words
    5. fix same-speaker segment overlap (cross-speaker preserved)

Passes 1–4 are skipped when the caller passes ``segment_level_only=True``
(used by ``web/server._sync_segment_words_with_text`` whose proportional
word redistribution should not be subjected to a per-word duration cap).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

from .policy import TimingPolicy

if TYPE_CHECKING:
    from models.transcript import TranscriptSegment, WordTimestamp


def estimate_max_word_duration(word_text: str, policy: TimingPolicy) -> float:
    """Estimate the maximum plausible spoken duration of a single word.

    For normal words, uses a character-count heuristic clamped between
    ``policy.duration_min`` and ``policy.duration_max`` so only clearly
    broken timestamps are affected.

    For **elongated** words (those with a run of
    ``policy.elongation_run_threshold`` or more repeated characters,
    e.g. ``noooooo``, ``BAKAAAAA``, ``STUPIDDDDD``), the estimate is
    raised by ``policy.elongation_per_char`` per character in the
    longest repeated run so an emotional shout/stretch is never
    incorrectly trimmed.
    """
    text = word_text.strip()
    if not text:
        return policy.duration_min

    chars = len(text)
    base = chars * policy.char_to_seconds + policy.base_seconds

    # Find the longest run of consecutive identical characters (case-insensitive)
    max_run = 1
    current_run = 1
    for i in range(1, len(text)):
        if text[i].lower() == text[i - 1].lower():
            current_run += 1
            if current_run > max_run:
                max_run = current_run
        else:
            current_run = 1

    if max_run >= policy.elongation_run_threshold:
        # Elongated / emotionally stretched word — allow much longer duration.
        # No hard upper cap because real shouts are routinely 2–4 s long.
        elongation_budget = max_run * policy.elongation_per_char
        return max(policy.duration_min, base + elongation_budget)

    # Normal word — clamp tightly so broken timestamps get caught.
    return max(policy.duration_min, min(policy.duration_max, base))


class Sanitizer:
    """Apply word- and segment-level timing fixes to a list of segments.

    Stateless apart from the :class:`TimingPolicy` reference, so a single
    instance can be reused across jobs.
    """

    def __init__(self, policy: TimingPolicy | None = None) -> None:
        self.policy = policy or TimingPolicy()

    # ── Public API ──────────────────────────────────────────────────────────

    def sanitize(
        self,
        segments: "list[TranscriptSegment]",
    ) -> "list[TranscriptSegment]":
        """Run all passes (word + segment level)."""
        return self._sanitize(segments, segment_level_only=False)

    def sanitize_segment_only(
        self,
        segments: "list[TranscriptSegment]",
    ) -> "list[TranscriptSegment]":
        """Skip word-level passes; only fix sort + same-speaker segment overlap."""
        return self._sanitize(segments, segment_level_only=True)

    # ── Implementation ──────────────────────────────────────────────────────

    def _sanitize(
        self,
        segments: "list[TranscriptSegment]",
        *,
        segment_level_only: bool,
    ) -> "list[TranscriptSegment]":
        if not segments:
            return segments

        # Pass 0 — sort segments chronologically.
        segments.sort(key=lambda s: (s.start, s.end))

        fixes_duration = 0
        fixes_overlap = 0
        word_count = 0

        if not segment_level_only:
            fixes_duration, fixes_overlap, word_count = self._run_word_passes(
                segments
            )

        fixes_speaker_overlap = self._run_segment_passes(segments)

        # Logging — only emit when there was something to fix so normal jobs
        # don't spam the console.
        if segment_level_only:
            if fixes_speaker_overlap:
                logger.info(
                    "sanitize_timestamps (segment-level): fixed {} "
                    "same-speaker segment overlap(s)",
                    fixes_speaker_overlap,
                )
        elif fixes_duration or fixes_overlap or fixes_speaker_overlap:
            logger.info(
                "sanitize_timestamps: fixed {} broken duration(s), "
                "{} same-speaker word overlap(s), {} same-speaker segment "
                "overlap(s) across {} word(s)",
                fixes_duration,
                fixes_overlap,
                fixes_speaker_overlap,
                word_count,
            )

        return segments

    # ── Word-level passes (1–4) ─────────────────────────────────────────────

    def _run_word_passes(
        self,
        segments: "list[TranscriptSegment]",
    ) -> tuple[int, int, int]:
        """Returns (fixes_duration, fixes_overlap, total_word_count)."""

        # Build a flat list of (word, speaker) tuples so the speaker check is
        # cheap and the original word-object identity is preserved (we mutate
        # ``end`` in place).
        all_words_with_sp: list[tuple["WordTimestamp", str]] = []
        for seg in segments:
            for w in seg.words:
                all_words_with_sp.append((w, seg.speaker))

        if not all_words_with_sp:
            return (0, 0, 0)

        word_count = len(all_words_with_sp)
        speech_rate_factor = self._estimate_speech_rate(all_words_with_sp)

        fixes_duration = self._cap_word_durations(
            all_words_with_sp, speech_rate_factor
        )
        fixes_overlap = self._fix_same_speaker_word_overlaps(all_words_with_sp)
        self._snap_segment_to_words(segments)

        return (fixes_duration, fixes_overlap, word_count)

    def _estimate_speech_rate(
        self,
        all_words_with_sp: list[tuple["WordTimestamp", str]],
    ) -> float:
        """Estimate global speech-rate factor as ``median(actual / estimate)``.

        The factor is **only allowed to grow** (≥
        ``policy.speech_rate_factor_min``) — we never tighten the cap
        below baseline because that's what was broken originally.
        """
        ratios: list[float] = []
        for w, _sp in all_words_with_sp:
            dur = w.end - w.start
            if dur <= 0:
                continue
            base_est = estimate_max_word_duration(w.word, self.policy)
            if base_est <= 0:
                continue
            ratios.append(dur / base_est)

        if not ratios:
            return self.policy.speech_rate_factor_min

        ratios.sort()
        median_ratio = ratios[len(ratios) // 2]
        factor = max(
            self.policy.speech_rate_factor_min,
            min(self.policy.speech_rate_factor_max, median_ratio),
        )
        if factor > self.policy.speech_rate_log_threshold:
            logger.debug(
                "sanitize_timestamps: detected fast/elongated speech, "
                "loosening duration cap by ×{:.2f}",
                factor,
            )
        return factor

    def _cap_word_durations(
        self,
        all_words_with_sp: list[tuple["WordTimestamp", str]],
        speech_rate_factor: float,
    ) -> int:
        """Cap broken or zero-length word durations.

        Returns the number of fixes applied.
        """
        fixes = 0
        for w, _sp in all_words_with_sp:
            est = estimate_max_word_duration(w.word, self.policy) * speech_rate_factor
            dur = w.end - w.start

            # Fix negative / zero duration
            if dur <= 0:
                w.end = round(w.start + min(est, self.policy.duration_min), 3)
                fixes += 1
                continue

            # Cap broken duration (word lasts absurdly longer than expected)
            if dur > est + self.policy.silence_cap:
                w.end = round(w.start + est, 3)
                fixes += 1
        return fixes

    @staticmethod
    def _fix_same_speaker_word_overlaps(
        all_words_with_sp: list[tuple["WordTimestamp", str]],
    ) -> int:
        """Trim ``end`` to the *next same-speaker word's* ``start``.

        Cross-speaker overlap (interruption) is intentionally preserved.

        Optimisation: the previous implementation was O(N**2) — for every
        word it scanned forwards looking for the next same-speaker word.
        On long jobs that's tens of millions of comparisons.  We instead
        bucket word indices by speaker once, then for each word jump
        directly to the next same-speaker index.  O(N) total.
        """
        fixes = 0

        # Build {speaker -> ordered list of indices} once.
        speaker_indices: dict[str, list[int]] = {}
        for idx, (_w, sp) in enumerate(all_words_with_sp):
            speaker_indices.setdefault(sp, []).append(idx)

        # For each speaker, walk the bucket pairwise.  Both lists are
        # already in chronological order because we appended while
        # iterating ``all_words_with_sp`` in order.
        for indices in speaker_indices.values():
            for k in range(len(indices) - 1):
                cur_idx = indices[k]
                nxt_idx = indices[k + 1]
                cur_w, _ = all_words_with_sp[cur_idx]
                nxt_w, _ = all_words_with_sp[nxt_idx]
                if cur_w.end > nxt_w.start:
                    cur_w.end = round(nxt_w.start, 3)
                    fixes += 1
        return fixes

    @staticmethod
    def _snap_segment_to_words(
        segments: "list[TranscriptSegment]",
    ) -> None:
        """Recalculate segment ``start``/``end`` from first/last word."""
        for seg in segments:
            if seg.words:
                seg.start = seg.words[0].start
                seg.end = seg.words[-1].end

    # ── Segment-level pass (5) ──────────────────────────────────────────────

    def _run_segment_passes(
        self,
        segments: "list[TranscriptSegment]",
    ) -> int:
        """Fix same-speaker segment overlap; return number of fixes."""
        fixes = 0
        speaker_segs: dict[str, list["TranscriptSegment"]] = {}
        for seg in segments:
            speaker_segs.setdefault(seg.speaker, []).append(seg)

        gap = self.policy.same_speaker_segment_gap
        min_dur = self.policy.minimum_segment_duration

        for _, sp_segs in speaker_segs.items():
            # Already sorted by start time (from Pass 0).
            for i in range(len(sp_segs) - 1):
                cur = sp_segs[i]
                nxt = sp_segs[i + 1]
                if cur.end > nxt.start:
                    new_end = round(nxt.start - gap, 3)
                    if new_end <= cur.start:
                        # Degenerate case: segments nearly coincide.
                        new_end = round(cur.start + min_dur, 3)
                    cur.end = new_end
                    # Trim the last word's end if it extends past the segment.
                    if cur.words and cur.words[-1].end > cur.end:
                        cur.words[-1].end = cur.end
                    fixes += 1
        return fixes
