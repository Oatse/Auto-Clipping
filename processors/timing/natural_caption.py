"""
processors.timing.natural_caption — Natural Caption Style pass.

Produces short-form-friendly subtitle output for ClipAuto's renderer:

1. **Drop trailing micro-punctuation** — removes a single trailing
   period or comma (``.``, ``,``, ``。``, ``、``) from each segment's
   display text and from the last word of its ``words`` list. ``?``
   and ``!`` are preserved (they convey intonation, not just sentence
   boundary). Trailing ellipsis (``...``) is preserved (intentional
   trailing-off). Punctuation in the middle of a segment is preserved
   as a reading micro-pause.

2. **Split long segments** — if a segment's display text exceeds
   ``max_line_chars`` characters, slice the underlying word list into
   2 (when text is up to 2 × max) or 3 (longer) consecutive
   sub-segments at word boundaries. Speaker, position overrides, and
   per-word ``WordTimestamp`` instances survive the slice unchanged
   so downstream timing / rendering keep working.

The two passes compose: split first (so each sub-segment is short
enough that punctuation stripping operates on a real terminal token),
strip second.

Designed to run AFTER :class:`processors.timing.Sanitizer` and BEFORE
the subtitle renderer in ``web.services.pipeline_runner``. Idempotent:
running twice over already-natural output is a no-op.

Why post-translation, pre-render:

- Source-language fragments have already been translated and grouped,
  so the punctuation we strip is the **target language's** trailing
  micro-punct, not the source's.
- Both Pycaps and ASS renderers consume the same
  :class:`~models.transcript.TranscriptSegment` shape, so a single
  pass covers both Auto-Subtitle and All In workspaces.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from models.transcript import TranscriptSegment, WordTimestamp


# Single trailing micro-punct chars that get stripped. ``?``, ``!``,
# CJK exclamation/question marks, and ellipsis are preserved.
_TRAILING_MICRO_PUNCT: frozenset[str] = frozenset((".", ",", "。", "、"))

# Word-boundary punctuation that we treat as natural reading-pause
# anchors when picking cut points inside a long segment. Mid-segment
# commas / colons / semicolons get a scoring bonus so the splitter
# prefers them over arbitrary word boundaries.
_CUT_PUNCT_ANCHORS: frozenset[str] = frozenset(",.;:?!、，。；：")

# Defaults — match the user-confirmed Natural Caption Style design:
# 24 chars per line, max 3 sub-segments. Tuned for vertical 9:16
# short-form layout.
DEFAULT_MAX_LINE_CHARS: int = 24
DEFAULT_MAX_LINES: int = 3


# ── Public API ──────────────────────────────────────────────────────────


def apply_natural_caption_style(
    segments: "list[TranscriptSegment]",
    *,
    drop_trailing_punct: bool = True,
    split_long_segments: bool = True,
    max_line_chars: int = DEFAULT_MAX_LINE_CHARS,
    max_lines: int = DEFAULT_MAX_LINES,
) -> "list[TranscriptSegment]":
    """Apply the Natural Caption Style passes.

    The passes run in this order:

    1. Split each segment whose text exceeds ``max_line_chars`` into
       2 or 3 word-aligned sub-segments. Skipped when
       ``split_long_segments`` is False.
    2. Strip a single trailing ``.``, ``,``, ``。``, or ``、`` from
       each (sub-)segment's display text and from its last word's
       ``.word`` string. Skipped when ``drop_trailing_punct`` is False.

    Args:
        segments: Subtitle segments to transform. The function returns
            a new list — possibly longer than the input when splitting
            produces sub-segments. Existing :class:`TranscriptSegment`
            instances are reused unchanged when no split is needed;
            new instances are created for sub-segments. Word-level
            mutation (last-word punct strip) happens in place on the
            owning :class:`~models.transcript.WordTimestamp` instance.
        drop_trailing_punct: If True (default), strip a single trailing
            micro-punct from each (sub-)segment.
        split_long_segments: If True (default), segments whose display
            text exceeds ``max_line_chars`` get split.
        max_line_chars: Per-sub-segment char threshold. Default 24.
        max_lines: Hard cap on sub-segments per original segment.
            Default 3 — anything beyond that hurts readability more
            than it helps.

    Returns:
        A new list. Order preserved. Empty input returns an empty list.
    """
    if not segments:
        return []

    if not (drop_trailing_punct or split_long_segments):
        # Caller wants the pass available but disabled — return a fresh
        # list so the caller can still treat the result as owned without
        # accidentally aliasing the input.
        return list(segments)

    # Pass 1 — split first.
    if split_long_segments:
        out: "list[TranscriptSegment]" = []
        for seg in segments:
            out.extend(_split_long_segment(seg, max_line_chars, max_lines))
    else:
        out = list(segments)

    n_split = len(out) - len(segments)

    # Pass 2 — strip trailing micro-punct on each (sub-)segment.
    n_text_stripped = 0
    n_word_stripped = 0
    if drop_trailing_punct:
        for seg in out:
            new_text = _strip_trailing_micropunct(seg.text)
            if new_text != seg.text:
                seg.text = new_text
                n_text_stripped += 1
            if seg.words:
                last_w = seg.words[-1]
                new_word = _strip_trailing_micropunct(last_w.word)
                if new_word != last_w.word:
                    last_w.word = new_word
                    n_word_stripped += 1

    if n_split or n_text_stripped or n_word_stripped:
        logger.info(
            "natural_caption_style: split {} long segment(s), "
            "stripped trailing micro-punct on {} text(s) and {} word(s)",
            n_split,
            n_text_stripped,
            n_word_stripped,
        )

    return out


# ── Pass 2 — trailing-punct strip ────────────────────────────────────────


def _strip_trailing_micropunct(text: str) -> str:
    """Strip exactly one trailing micro-punct from ``text``.

    Preserves:

    - empty / whitespace-only input (returned unchanged)
    - ``?``, ``!``, ``！``, ``？`` (intonation markers)
    - ``...`` ellipsis (3+ dots — intentional trailing-off)
    - mid-text punctuation (kept as reading pauses)

    Idempotent: running twice produces the same result as once.
    """
    if not text:
        return text
    stripped = text.rstrip()
    if not stripped:
        return text
    last = stripped[-1]
    if last not in _TRAILING_MICRO_PUNCT:
        return stripped
    # Don't eat the tail of an ellipsis ("trailing off..." stays as-is).
    if last == "." and stripped.endswith("..."):
        return stripped
    return stripped[:-1].rstrip()


# ── Pass 1 — long-segment split ─────────────────────────────────────────


def _split_long_segment(
    seg: "TranscriptSegment",
    max_chars: int,
    max_lines: int,
) -> "list[TranscriptSegment]":
    """Slice ``seg`` into 2 or 3 word-aligned sub-segments when its
    text exceeds ``max_chars``. Returns ``[seg]`` unchanged if no
    split is needed or possible.

    The sub-segment display text is derived from ``seg.text`` (the
    translated text written by Phase 2), NOT from joining ``seg.words``.
    The ``words`` list still carries the source-language tokens from
    ElevenLabs because translation is non-aligned per-word — using them
    for display would leak the source language into the rendered
    subtitle. Time slicing still uses ``seg.words`` so per-word
    timestamps and karaoke timing stay accurate.
    """
    text = seg.text or ""
    n_chars = len(text)
    if n_chars <= max_chars:
        return [seg]

    # Need word-level data to split — without anchors we can't decide
    # where each sub-segment starts/ends in time.
    if not seg.words or len(seg.words) < 2:
        return [seg]

    # 2 sub-segments when text fits in two lines, otherwise 3.
    if n_chars <= 2 * max_chars:
        n_lines = 2
    else:
        n_lines = min(max_lines, 3)
    n_lines = min(n_lines, len(seg.words))
    if n_lines < 2:
        return [seg]

    # Translated text tokens — these drive the display split. When the
    # translation is in a CJK language with no whitespace tokens, we
    # fall back to splitting at the source-word boundaries (preserving
    # the legacy behaviour for CJK→CJK pipelines that don't have a
    # source/target language mismatch).
    text_tokens = text.split()
    if len(text_tokens) < n_lines:
        # Not enough tokens to fill ``n_lines`` sub-segments without
        # producing an empty one. Keep the segment whole rather than
        # silently injecting blanks or — worse — falling back to the
        # source-language ``seg.words`` for display text.
        return [seg]

    word_cuts = _find_balanced_cuts(seg.words, n_lines)
    if not word_cuts:
        return [seg]

    text_cuts = _find_balanced_string_cuts(text_tokens, n_lines)
    if not text_cuts or len(text_cuts) != len(word_cuts):
        return [seg]

    word_boundaries = [0, *word_cuts, len(seg.words)]
    text_boundaries = [0, *text_cuts, len(text_tokens)]

    out: "list[TranscriptSegment]" = []
    for k in range(len(word_boundaries) - 1):
        wi, wj = word_boundaries[k], word_boundaries[k + 1]
        ti, tj = text_boundaries[k], text_boundaries[k + 1]
        if wi >= wj or ti >= tj:
            continue
        sub_words = list(seg.words[wi:wj])
        sub_text = " ".join(text_tokens[ti:tj]).strip()
        if not sub_text:
            continue
        out.append(
            replace(
                seg,
                start=round(sub_words[0].start, 3),
                end=round(sub_words[-1].end, 3),
                text=sub_text,
                words=sub_words,
            )
        )

    return out or [seg]


def _find_balanced_cuts(
    words: "list[WordTimestamp]",
    n_lines: int,
) -> list[int]:
    """Pick ``n_lines - 1`` word indices to split at.

    Strategy:

    - Compute cumulative char positions per word ending (including
      one space between words).
    - Distribute targets evenly: ``total * k / n_lines`` for
      ``k = 1..n_lines-1``.
    - For each target, search the surrounding word window for the
      best cut: a word whose trailing char is in
      :data:`_CUT_PUNCT_ANCHORS` gets a 30 % distance bonus, otherwise
      pure distance to target wins.
    - Cuts are returned strictly increasing; no zero-length lines.
    """
    n = len(words)
    if n < n_lines:
        # One-word-per-line fallback. Caller guarantees n_lines >= 2.
        return list(range(1, n))

    # Cumulative char positions at the END of each word (with spaces
    # between consecutive words counted once).
    cum_ends: list[int] = []
    acc = 0
    for i, w in enumerate(words):
        acc += len(w.word or "")
        cum_ends.append(acc)
        if i < n - 1:
            acc += 1
    total = acc if acc > 0 else 1

    targets = [total * (k + 1) / n_lines for k in range(n_lines - 1)]
    cuts: list[int] = []

    for tgt in targets:
        floor = (cuts[-1] + 1) if cuts else 1
        ceil = n - 1
        if floor > ceil:
            break

        best_cut = floor
        best_score = float("inf")
        for i in range(floor - 1, ceil):
            cut_after = i + 1
            if cut_after <= (cuts[-1] if cuts else 0):
                continue
            tail = (words[i].word or "").rstrip()
            ends_punct = bool(tail) and tail[-1] in _CUT_PUNCT_ANCHORS
            dist = abs(cum_ends[i] - tgt)
            score = dist * (0.7 if ends_punct else 1.0)
            if score < best_score:
                best_score = score
                best_cut = cut_after

        cuts.append(best_cut)

    return cuts


def _find_balanced_string_cuts(
    tokens: list[str],
    n_lines: int,
) -> list[int]:
    """Pick ``n_lines - 1`` token indices to split the translated text at.

    Mirrors :func:`_find_balanced_cuts` but operates on plain string
    tokens (post-translation) instead of :class:`WordTimestamp` objects.
    The two splits run independently and are zipped together by the
    caller — they don't have to land on the "same" word index, just
    produce ``n_lines`` chunks from each side.

    Returns a strictly-increasing list of cut indices in
    ``[1, len(tokens) - 1]``. Returns ``[]`` only if a valid split
    isn't possible (caller falls back to leaving the segment whole).
    """
    n = len(tokens)
    if n < n_lines:
        return list(range(1, n))

    cum_ends: list[int] = []
    acc = 0
    for i, tok in enumerate(tokens):
        acc += len(tok or "")
        cum_ends.append(acc)
        if i < n - 1:
            acc += 1
    total = acc if acc > 0 else 1

    targets = [total * (k + 1) / n_lines for k in range(n_lines - 1)]
    cuts: list[int] = []

    for tgt in targets:
        floor = (cuts[-1] + 1) if cuts else 1
        ceil = n - 1
        if floor > ceil:
            break

        best_cut = floor
        best_score = float("inf")
        for i in range(floor - 1, ceil):
            cut_after = i + 1
            if cut_after <= (cuts[-1] if cuts else 0):
                continue
            tail = (tokens[i] or "").rstrip()
            ends_punct = bool(tail) and tail[-1] in _CUT_PUNCT_ANCHORS
            dist = abs(cum_ends[i] - tgt)
            score = dist * (0.7 if ends_punct else 1.0)
            if score < best_score:
                best_score = score
                best_cut = cut_after

        cuts.append(best_cut)

    return cuts


__all__ = [
    "apply_natural_caption_style",
    "DEFAULT_MAX_LINE_CHARS",
    "DEFAULT_MAX_LINES",
]
