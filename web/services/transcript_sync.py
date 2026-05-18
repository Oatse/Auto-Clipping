"""
web.services.transcript_sync — Helpers for keeping segment-level text
in sync with word-level entries.

When the user edits a segment's text in the preview (or Gemini refines
it), the ``text`` field changes but the ``words`` list still contains
the original ElevenLabs words.  The Pycaps word-pop renderer reads from
``words``, so without this sync the rendered subtitles would show the
old text.
"""

from __future__ import annotations

from models.transcript import TranscriptSegment, WordTimestamp


def sync_segment_words_with_text(seg: TranscriptSegment) -> None:
    """Sync ``seg.words[]`` with ``seg.text``.

    Strategy:
    - Split ``seg.text`` into new tokens.
    - If the token count matches the existing ``words`` count, update
      each word's ``.word`` field in place (preserving timestamps).
    - If the token count differs, redistribute the segment's time span
      proportionally across the new tokens.
    """
    new_words = seg.text.strip().split()
    if not new_words:
        return

    old_words = seg.words or []

    # No-op when the text already matches the existing word list.
    if len(old_words) == len(new_words):
        all_match = all(
            ow.word.strip().lower() == nw.strip().lower()
            for ow, nw in zip(old_words, new_words)
        )
        if all_match:
            return

    if len(old_words) == len(new_words):
        # Same word count: just update the word text, keep timestamps.
        for ow, nw in zip(old_words, new_words):
            ow.word = nw
        return

    # Different word count: redistribute timestamps proportionally.
    seg_start = seg.start
    seg_end = seg.end
    seg_duration = seg_end - seg_start
    n = len(new_words)
    word_dur = seg_duration / n if n > 0 else 0

    new_word_list: list[WordTimestamp] = []
    for i, w in enumerate(new_words):
        ws = round(seg_start + i * word_dur, 3)
        we = round(seg_start + (i + 1) * word_dur, 3)
        new_word_list.append(WordTimestamp(word=w, start=ws, end=we))
    seg.words = new_word_list
