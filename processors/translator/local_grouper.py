"""
processors.translator.local_grouper — Heuristic word-to-segment grouper
used as a fallback when Gemini is unavailable.

Splits a flat ``WordTimestamp`` stream into subtitle segments at
speaker changes, long pauses (>0.7 s), sentence boundaries (after
``.?!`` once at least 3 words have accumulated), and a 12-word hard
cap.  Mirrors the behaviour Gemini aims for so the fallback path
produces visually similar subtitles.
"""

from __future__ import annotations

from models.transcript import TranscriptSegment, WordTimestamp


def local_group_words(
    words: list[WordTimestamp],
    speakers: list[str],
) -> list[TranscriptSegment]:
    """Group words using pause/speaker/sentence heuristics (no Gemini)."""
    if not words:
        return []

    segments: list[TranscriptSegment] = []
    cur: list[WordTimestamp] = []
    cur_sp = speakers[0]

    for _, (w, sp) in enumerate(zip(words, speakers)):
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


def local_group_from_segments(
    segments: list[TranscriptSegment],
) -> list[TranscriptSegment]:
    """Flatten segments to word-level then regroup with local heuristics."""
    all_words: list[WordTimestamp] = []
    speakers: list[str] = []
    for seg in segments:
        for w in seg.words:
            all_words.append(w)
            speakers.append(seg.speaker)

    if not all_words:
        return segments

    return local_group_words(all_words, speakers)
