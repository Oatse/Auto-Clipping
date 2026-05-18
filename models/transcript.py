"""
models/transcript.py — Data contracts for the pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from loguru import logger


# ─── Phase 1 Output ───────────────────────────────────────────────────────────

@dataclass
class WordTimestamp:
    """A single word with its precise start/end timestamps from the STT engine."""
    word: str
    start: float
    end: float
    score: float = 0.0  # Alignment confidence score
    source: str = ""    # Provenance: "elevenlabs" | "interpolated" | ""

    @property
    def duration(self) -> float:
        return self.end - self.start

    def to_dict(self) -> dict[str, Any]:
        d = {
            "word": self.word,
            "start": self.start,
            "end": self.end,
            "score": self.score,
        }
        if self.source:
            d["source"] = self.source
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WordTimestamp":
        return cls(
            word=data["word"],
            start=data["start"],
            end=data["end"],
            score=data.get("score", 0.0),
            source=data.get("source", ""),
        )


@dataclass
class TranscriptSegment:
    """
    A single speech segment from the ElevenLabs STT pipeline.
    Retains start/end/text/speaker through all pipeline phases.
    """
    start: float
    end: float
    text: str
    speaker: str
    words: list[WordTimestamp] = field(default_factory=list)
    pos_x: float | None = None        # Per-segment X position override (0-100%)
    pos_y: float | None = None        # Per-segment Y position override (0-100%)
    pos_override: bool = False         # Whether to use custom position

    @property
    def duration(self) -> float:
        return self.end - self.start

    def to_dict(self) -> dict[str, Any]:
        d = {
            "start": self.start,
            "end": self.end,
            "text": self.text,
            "speaker": self.speaker,
            "words": [w.to_dict() for w in self.words],
        }
        if self.pos_override:
            d["pos_x"] = self.pos_x
            d["pos_y"] = self.pos_y
            d["pos_override"] = True
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TranscriptSegment":
        return cls(
            start=data["start"],
            end=data["end"],
            text=data["text"],
            speaker=data.get("speaker", "SPEAKER_00"),
            words=[WordTimestamp.from_dict(w) for w in data.get("words", [])],
            pos_x=data.get("pos_x"),
            pos_y=data.get("pos_y"),
            pos_override=data.get("pos_override", False),
        )


# ─── Phase 3 Output ───────────────────────────────────────────────────────────

@dataclass
class PycapsWordEntry:
    """
    A single word entry in the Pycaps-compatible subtitle JSON.
    Uses global video timestamps (not relative to segment).
    """
    word: str
    global_start: float     # Segment_Start_In_Video + Word_Start
    global_end: float       # Segment_Start_In_Video + Word_End

    def to_dict(self) -> dict[str, Any]:
        return {
            "word": self.word,
            "start": self.global_start,
            "end": self.global_end,
        }


# ─── Timestamp Sanitisation ──────────────────────────────────────────────────

# Default silence cap: if a word's duration creates > this many seconds of
# dead silence before the next speech, the end time is considered broken.
_DEFAULT_SILENCE_CAP = 2.0


def _estimate_max_word_duration(word_text: str) -> float:
    """Estimate the maximum plausible spoken duration of a word.

    For normal words, uses a character-count heuristic clamped to 1.5 s so
    that only clearly broken timestamps are affected.

    For **elongated** words (those with a run of ≥ 3 repeated characters,
    e.g. "noooooo", "BAKAAAAAAA", "STUPIDDDDDD"), the estimate is raised
    significantly — 0.35 s per character in the longest repeated run —
    so that a genuine emotional shout/stretch is never incorrectly trimmed.

    Examples
    --------
    "baka"         → 0.51 s   (normal word,   3 chars — short)
    "Hello"        → 0.60 s   (normal word,   5 chars)
    "beautiful"    → 0.96 s   (normal word,  10 chars)
    "noooooo"      → estimated run=5 → 0.78 + 5×0.35 = 2.53 s
    "BAKAAAAAAAAAA"→ longest run=9 A's → 1.32 + 9×0.35 = 4.47 s
    "STUPIDDDDDDD" → longest run=7 D's → 1.08 + 7×0.35 = 3.53 s
    """
    text = word_text.strip()
    if not text:
        return 0.3

    chars = len(text)
    base = chars * 0.09 + 0.15

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

    if max_run >= 3:
        # Elongated / emotionally stretched word — allow much longer duration
        elongation_budget = max_run * 0.35
        return max(0.3, base + elongation_budget)   # no hard upper cap here

    # Normal word — clamp tightly so broken timestamps get caught
    return max(0.3, min(1.5, base))


def sanitize_timestamps(
    segments: list[TranscriptSegment],
    silence_cap: float = _DEFAULT_SILENCE_CAP,
    segment_level_only: bool = False,
) -> list[TranscriptSegment]:
    """Fix broken timestamps where subtitles linger after the speaker stops.

    This targets only clearly broken data — normal timing is left untouched.
    The sanitizer is **speaker-aware**: cross-speaker overlaps (one speaker
    interrupting another) are preserved as natural conversation, while
    same-speaker overlaps (which can never be physically simultaneous from
    one mouth) are trimmed.

    Corrections applied (in order):
      1. **Word duration cap** — if a single word's duration exceeds the
         character-based estimate **plus** *silence_cap* seconds, its ``end``
         is capped to the estimated duration.  A 5-letter word like "Hello"
         gets an estimate of ~0.60 s; it would only be capped if its actual
         duration exceeds 0.60 + 2.0 = 2.60 s, leaving a wide safety margin.
      2. **Same-speaker word overlap fix** — if a word's ``end`` exceeds the
         next *same-speaker* word's ``start`` (across segment boundaries
         within the same speaker), it is trimmed.  Cross-speaker overlap
         is preserved (interruption).
      3. **Segment boundary recalculation** — segment ``start``/``end`` are
         re-derived from the (now-corrected) first/last word timestamps so
         the subtitle appears and disappears exactly when speech happens.
      4. **Same-speaker segment overlap prevention** — if a segment's
         ``end`` still exceeds the *next same-speaker* segment's ``start``,
         it is trimmed with a 10 ms gap.  Cross-speaker segment overlap is
         intentionally allowed so simultaneous talkers render side-by-side.

    When ``segment_level_only=True``, only passes 0 (sort) and 4 (same-speaker
    segment overlap) are run.  Passes 1–3 (word duration cap, word overlap
    fix, segment boundary recalculation from words) are skipped.  This is
    intended for segments whose word timestamps were artificially
    redistributed (e.g. by ``_sync_segment_words_with_text``) and should not
    be subjected to per-word duration analysis.

    Parameters
    ----------
    segments:
        Transcript segments to sanitize (modified **in-place**).
    silence_cap:
        Maximum tolerated silence (seconds) appended to a word's estimated
        duration before its ``end`` is considered broken.  Default 2.0 s.
    segment_level_only:
        When True, skip word-level sanitization (passes 1–3).  Only fix
        segment ordering and same-speaker overlap.  Default False.

    Returns
    -------
    list[TranscriptSegment]
        The same list, sanitized.
    """
    if not segments:
        return segments

    # ── Pass 0: sort segments by start time ────────────────────────────────
    segments.sort(key=lambda s: (s.start, s.end))

    fixes_duration = 0
    fixes_overlap = 0
    all_words_count = 0
    speech_rate_factor = 1.0  # multiplier on the duration estimate

    # ── Passes 1–3: word-level fixes (skipped in segment_level_only mode) ─
    if not segment_level_only:
        # Build a flat list of (word, speaker) tuples so the speaker check is
        # cheap and the original word-object identity is preserved (we mutate
        # ``end`` in place).
        all_words_with_sp: list[tuple[WordTimestamp, str]] = []
        for seg in segments:
            for w in seg.words:
                all_words_with_sp.append((w, seg.speaker))

        if not all_words_with_sp:
            return segments

        all_words_count = len(all_words_with_sp)

        # ── Pass 1a: estimate global speech-rate factor ───────────────────
        # Compare each word's actual duration with the character-based
        # estimate.  Take the median ratio so a few elongated words don't
        # dominate.  The factor is **only allowed to grow** (≥ 1.0) — we
        # never tighten the cap below baseline because that's what was
        # already broken in the first place.
        ratios: list[float] = []
        for w, _sp in all_words_with_sp:
            dur = w.end - w.start
            if dur <= 0:
                continue
            base_est = _estimate_max_word_duration(w.word)
            if base_est <= 0:
                continue
            ratios.append(dur / base_est)
        if ratios:
            ratios.sort()
            median_ratio = ratios[len(ratios) // 2]
            # Clamp: never tighten (≥1.0), never blow up unbounded (≤3.0)
            speech_rate_factor = max(1.0, min(3.0, median_ratio))
            if speech_rate_factor > 1.05:
                logger.debug(
                    "sanitize_timestamps: detected fast/elongated speech, "
                    "loosening duration cap by ×{:.2f}",
                    speech_rate_factor,
                )

        # ── Pass 1b: per-word duration cap (rate-adjusted) ────────────────
        for w, _sp in all_words_with_sp:
            est = _estimate_max_word_duration(w.word) * speech_rate_factor
            dur = w.end - w.start

            # Fix negative / zero duration
            if dur <= 0:
                w.end = round(w.start + min(est, 0.3), 3)
                fixes_duration += 1
                continue

            # Cap broken duration (word lasts absurdly longer than expected)
            if dur > est + silence_cap:
                w.end = round(w.start + est, 3)
                fixes_duration += 1

        # ── Pass 2: same-speaker word overlap fix ─────────────────────────
        # Walk forward; for each word find the next same-speaker word and
        # trim ``end`` to its ``start`` if they overlap.  Cross-speaker
        # overlap (interruption) is intentionally preserved.
        for i, (w, sp) in enumerate(all_words_with_sp):
            for j in range(i + 1, len(all_words_with_sp)):
                nxt_w, nxt_sp = all_words_with_sp[j]
                if nxt_sp != sp:
                    continue
                if w.end > nxt_w.start:
                    w.end = round(nxt_w.start, 3)
                    fixes_overlap += 1
                # First same-speaker successor is enough; further ones are
                # already after it chronologically.
                break

        # ── Pass 3: recalculate segment boundaries from words ─────────────
        for seg in segments:
            if seg.words:
                seg.start = seg.words[0].start
                seg.end = seg.words[-1].end

    # ── Pass 4: prevent same-speaker segment overlaps ─────────────────────
    # Group segments by speaker then ensure no two segments from the same
    # speaker overlap in time.  When overlap is found the earlier segment's
    # end is trimmed to the later segment's start.  A tiny gap (10 ms) is
    # kept so subtitle renderers treat them as distinct, non-overlapping
    # entries.  Cross-speaker overlap is **intentionally preserved** — when
    # two speakers talk over each other the renderer stacks their lines.
    fixes_speaker_overlap = 0
    speaker_segs: dict[str, list["TranscriptSegment"]] = {}
    for seg in segments:
        speaker_segs.setdefault(seg.speaker, []).append(seg)

    for speaker, sp_segs in speaker_segs.items():
        # Already sorted by start time (from Pass 0)
        for i in range(len(sp_segs) - 1):
            cur = sp_segs[i]
            nxt = sp_segs[i + 1]
            if cur.end > nxt.start:
                # Trim current segment's end to just before the next one starts
                gap = 0.01  # 10 ms gap
                new_end = round(nxt.start - gap, 3)
                if new_end <= cur.start:
                    # Degenerate case: segments nearly coincide — set minimal duration
                    new_end = round(cur.start + 0.05, 3)
                cur.end = new_end
                # Also trim the last word's end if it extends past the segment
                if cur.words and cur.words[-1].end > cur.end:
                    cur.words[-1].end = cur.end
                fixes_speaker_overlap += 1

    if segment_level_only:
        if fixes_speaker_overlap:
            logger.info(
                "sanitize_timestamps (segment-level): fixed {} same-speaker "
                "segment overlap(s)",
                fixes_speaker_overlap,
            )
    else:
        if fixes_duration or fixes_overlap or fixes_speaker_overlap:
            logger.info(
                "sanitize_timestamps: fixed {} broken duration(s), "
                "{} same-speaker word overlap(s), {} same-speaker segment "
                "overlap(s) across {} word(s)",
                fixes_duration,
                fixes_overlap,
                fixes_speaker_overlap,
                all_words_count,
            )

    return segments
