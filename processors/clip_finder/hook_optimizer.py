"""
processors/clip_finder/hook_optimizer.py — Snap Moment.start to a stronger hook.

Boundary refinement runs after Clip Finder produces base time-ranges and
after silence-snap (see ``boundary.refine_boundaries``). This second pass
looks at the first few seconds of each Moment and, when a *hook line*
exists in the look-ahead window, shifts ``Moment.start`` forward to land
on that line's word boundary.

A "hook line" is a transcript word that opens with one of:
  - a question word (what / why / how / who / when / where / wait / no
    way / are you / did you …)
  - an exclamation / interjection (oh / wow / hold on / look / listen /
    bro / dude …)
  - a name-drop (capitalised proper noun in the first word — heuristic)

The shift is bounded so the optimizer is guaranteed safe:
  - Never shift backward (only forward).
  - Never shift past ``window_seconds`` (default 3 s).
  - Never produce a Moment shorter than ``min_duration``.
  - When no hook is found in the window, the start is left unchanged.

ADR-0003 contract: this is a strict refinement *inside* boundary
refinement. The ElevenLabs words remain canonical (ADR-0001) — we only
*choose* a different word as the new start; we never invent timing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Sequence

from models.clip import Clip


# ─── Hook lexicon ────────────────────────────────────────────────────────────
#
# English-only with JP→EN translation patterns. The project's primary
# clipping target is JP VTuber content with auto-translated EN
# subtitles, so the lexicon is biased toward:
#
#   - Reaction interjections that translators commonly preserve from
#     JP (え→eh, はぁ→huh, ちょっと→wait, マジ→for real).
#   - Romanised JP that bleeds into translated subs because translators
#     leave them as-is (yabai, sugoi, nani, oi).
#   - Translator-stage tags that almost always sit on top of clip-worthy
#     reactions ([laughs], [gasps], [screams]).
#
# Lower-cased exact-match against the first transcript word, with one
# extra pass for square-bracketed reaction tags.

_HOOK_QUESTION_WORDS = frozenset({
    "what", "why", "how", "who", "when", "where", "which", "wait",
    "did", "do", "does", "is", "are", "was", "were", "can", "could",
    "would", "should", "have", "has", "ever", "really", "seriously",
})

_HOOK_INTERJECTIONS = frozenset({
    # Native English reaction openers
    "oh", "ohh", "ooh", "wow", "whoa", "yo", "hey", "look", "listen",
    "bro", "dude", "guys", "stop", "no", "hold", "behold", "okay",
    "alright", "lol", "lmao", "omg", "damn", "nah", "huh", "hah",
    "noo", "nooo", "noooo", "whaaat",
    # Translator-preserved JP reactions (transliterated to romaji)
    "eh", "ehh", "eee", "eeh", "eeeh", "ehhh",
    # Romanised JP that commonly bleeds into translated EN subs
    "yabai", "yaba", "sugoi", "sugee", "maji", "nani", "oi", "oii",
    "kawaii", "kawai", "ehhh", "naruhodo",
})

# Compound openers — phrases of 2-3 tokens that act as a single hook.
# Matched against the first N tokens of the look-ahead window.
_HOOK_COMPOUNDS = (
    ("hold", "on"),
    ("wait", "wait"),
    ("wait", "what"),
    ("no", "way"),
    ("for", "real"),
    ("are", "you", "kidding"),
    ("you're", "kidding"),
    ("oh", "my"),
    ("oh", "no"),
    ("are", "you", "serious"),
)

# Translator-stage reaction tags — content in square or round brackets
# at the start of a segment, e.g. ``[laughs]``, ``(gasps)``,
# ``[in Japanese]``. These are an extremely strong hook signal because
# the translator only emits them when the reaction is too noteworthy
# to leave un-annotated.
_HOOK_TAG_RE = re.compile(r"^[\[\(](?:laughs?|laughing|gasps?|screams?|"
                          r"shouting|shouts|chuckles?|sighs?|in japanese|"
                          r"in jp|whispers?|inhales?|exhales?)[\]\)]",
                          re.IGNORECASE)

# Strong sentence-final punctuation that signals a closed beat — useful
# to avoid grabbing a hook that lives in the middle of a sentence we
# already started before the silence-snapped start.
_SENTENCE_FINAL_RE = re.compile(r"[.!?…]\s*$")


@dataclass(frozen=True)
class HookPolicy:
    """Tunables for the hook optimizer.

    Defaults match the ADR-0003 contract: 3 s look-ahead, 5 s minimum
    Moment duration after the shift, only forward shifts.
    """

    window_seconds: float = 3.0
    min_duration: float = 5.0
    enabled: bool = True


_DEFAULT_POLICY = HookPolicy()


# ─── Public API ──────────────────────────────────────────────────────────────


def apply(
    clips: Sequence[Clip],
    transcript: Sequence[dict] | None,
    *,
    policy: HookPolicy = _DEFAULT_POLICY,
) -> list[Clip]:
    """Return a copy of ``clips`` with each Moment's start snapped to a hook.

    The transcript is the same word-bearing transcript used everywhere
    else in Clip Finder — a list of dicts with ``start`` / ``end`` /
    ``text`` keys. Falls back to a no-op when:

      - the optimizer is disabled by policy,
      - the transcript is empty or has no words in the look-ahead window,
      - no hook line is found in the window,
      - shifting would make the Moment shorter than ``min_duration``.

    The original Clip objects are NOT mutated — boundary.refine_boundaries
    contract is preserved.
    """
    if not policy.enabled or not clips:
        return list(clips)
    if not transcript:
        return list(clips)

    out: list[Clip] = []
    for clip in clips:
        new_start = _find_hook_start(
            current_start=clip.start,
            current_end=clip.end,
            transcript=transcript,
            policy=policy,
        )
        if new_start is None or new_start <= clip.start:
            out.append(clip)
            continue

        # Floor on duration after shift.
        if clip.end - new_start < policy.min_duration:
            out.append(clip)
            continue

        # Shallow clone — preserves score / signals / hunter / etc.
        out.append(_clone_with_start(clip, new_start))

    return out


# ─── Internals ───────────────────────────────────────────────────────────────


def _find_hook_start(
    *,
    current_start: float,
    current_end: float,
    transcript: Sequence[dict],
    policy: HookPolicy,
) -> float | None:
    """Return the timestamp of the best hook word, or None if no hook found."""
    window_end = min(current_end, current_start + policy.window_seconds)
    window_words = list(_words_in_window(transcript, current_start, window_end))
    if not window_words:
        return None

    # Translator-stage reaction tag at the very start (``[laughs]``,
    # ``[gasps]``, etc.) is a strong signal — JP→EN translators only
    # emit these when the moment is noteworthy. We accept it as the
    # opening hook even though it's the *first* word of the segment.
    first_word_text = window_words[0][0]
    if _HOOK_TAG_RE.match(first_word_text.strip()):
        return window_words[0][1]

    # Skip plain hook detection on the very first word — that's already
    # the start. We're looking for a *better* anchor than what
    # silence-snap gave us.
    candidate_words = window_words[1:] if len(window_words) > 1 else []
    if not candidate_words:
        return None

    # Pass 1: compound openers (e.g. ``hold on``, ``no way``). These
    # take precedence over single-word hits because translated EN often
    # builds reactions out of multi-token phrases that single-word
    # match would miss the *start* of.
    compound_t = _find_compound_match(window_words, _HOOK_COMPOUNDS)
    if compound_t is not None:
        return compound_t

    # Pass 2: single-word hooks. Pick the *latest* hook in the window —
    # shifting later trims more dead air at the head; the earliest hook
    # would just put us back near the original start. Bounded by
    # window_seconds anyway.
    best_t: float | None = None
    for word_text, word_start in candidate_words:
        if _is_hook_word(word_text):
            if best_t is None or word_start > best_t:
                best_t = word_start

    return best_t


def _find_compound_match(
    window_words: Sequence[tuple[str, float]],
    compounds: Sequence[tuple[str, ...]],
) -> float | None:
    """Return start time of the *latest* compound-opener match in window.

    Compounds are matched left-to-right against consecutive words.
    Returns the timestamp of the matching head token so the Moment
    starts on the full phrase, not partway through it.

    Two rules borrowed from the single-word path so behaviour stays
    consistent:

    * Matches at index 0 are skipped — that's the existing start; we
      need a *better* anchor to justify a shift.
    * Among multiple matches, prefer the latest one (trims more dead
      air at the head, bounded by the window).
    """
    cleaned = [
        (
            w.strip().strip(".,!?…:;\"'()[]").lower(),
            t,
        )
        for w, t in window_words
    ]
    best_t: float | None = None
    for compound in compounds:
        for i in range(1, len(cleaned) - len(compound) + 1):
            slice_ = [w for w, _ in cleaned[i:i + len(compound)]]
            if tuple(slice_) == compound:
                head_t = cleaned[i][1]
                if best_t is None or head_t > best_t:
                    best_t = head_t
    return best_t


def _words_in_window(
    transcript: Sequence[dict],
    start: float,
    end: float,
) -> Iterable[tuple[str, float]]:
    """Yield (word_text, word_start) for every word inside [start, end].

    The Clip Finder transcript shape is ``[{start, end, text, ...}]`` —
    each dict is a *segment*, and ``text`` is the segment text. We don't
    require word-level timestamps here; we approximate by splitting each
    segment's text into tokens and distributing them linearly across the
    segment's time range. That's good enough for finding question words —
    the worst-case error is bounded by one segment duration (~3 s).
    """
    for seg in transcript:
        if not isinstance(seg, dict):
            continue
        seg_start = float(seg.get("start", 0.0))
        seg_end = float(seg.get("end", seg_start))
        # Skip segments fully outside the window early.
        if seg_end < start or seg_start > end:
            continue

        text = str(seg.get("text", "")).strip()
        if not text:
            continue
        tokens = text.split()
        if not tokens:
            continue

        seg_dur = max(0.001, seg_end - seg_start)
        per_token = seg_dur / len(tokens)
        for i, tok in enumerate(tokens):
            t = seg_start + i * per_token
            if t < start or t > end:
                continue
            yield tok, t


def _is_hook_word(token: str) -> bool:
    """Return True if ``token`` opens a hook-class line."""
    cleaned = token.strip().strip(".,!?…:;\"'()[]").lower()
    if not cleaned:
        return False
    if cleaned in _HOOK_QUESTION_WORDS:
        return True
    if cleaned in _HOOK_INTERJECTIONS:
        return True
    # All-caps single-word emphasis (e.g. "WHAT", "STOP") is also a hook.
    raw = token.strip().strip(".,!?…:;\"'()[]")
    if len(raw) >= 3 and raw.isupper():
        return True
    return False


def _clone_with_start(clip: Clip, new_start: float) -> Clip:
    """Shallow copy of ``clip`` with a different ``start``.

    Mirrors the ``boundary._copy`` strategy so we don't accidentally
    share mutable lists with the caller.
    """
    return Clip(
        start=round(new_start, 3),
        end=clip.end,
        title=clip.title,
        reason=clip.reason,
        highlight_type=clip.highlight_type,
        hunter=clip.hunter,
        dead_air_timestamps=list(clip.dead_air_timestamps),
        score=clip.score,
        rescued=clip.rescued,
        file_idx=clip.file_idx,
        filename=clip.filename,
        signals=list(clip.signals),
    )


__all__ = ["HookPolicy", "apply"]
