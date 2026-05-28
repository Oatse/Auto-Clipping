"""
processors.stt.base — Protocol every speech-to-text engine must satisfy.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from models.transcript import TranscriptSegment


@runtime_checkable
class SttEngine(Protocol):
    """Speech-to-Text engine contract.

    Implementations are responsible for:
        1. Extracting audio from the video (or accepting raw audio).
        2. Calling the underlying engine.
        3. Returning a list of :class:`TranscriptSegment` with word-level
           timestamps populated.
        4. Persisting any debug artifacts (raw response, intermediate
           JSON) under ``output_dir``.

    Implementations should NOT mutate timestamps for "cleanup" purposes —
    that is :mod:`processors.timing`'s responsibility.  An engine MAY run
    the canonical sanitizer once before returning, but it must save a
    pre-sanitization snapshot under ``output_dir`` so the Preview UI can
    show what the engine actually reported.
    """

    async def transcribe(
        self,
        video_path: Path | str,
        output_dir: Path | str,
        *,
        speaker_detection: bool = True,
        num_speakers: int | None = None,
        language_code: str | None = None,
    ) -> tuple[list[TranscriptSegment], Path]:
        """Run the full STT pipeline.

        Parameters
        ----------
        video_path:
            Source video file.
        output_dir:
            Directory where intermediate audio + JSON snapshots will be
            written.
        speaker_detection:
            When True the engine should attempt diarisation.  When False
            every segment must be tagged ``SPEAKER_00``.
        num_speakers:
            Optional hint for the maximum speaker count.  Engines may
            ignore it if they don't support it.
        language_code:
            Optional ISO-639-1 / ISO-639-3 hint for the source-language.
            When supplied, accuracy improves on noisy audio because the
            engine doesn't have to detect language first.  Engines that
            don't accept the hint may ignore it.

        Returns
        -------
        tuple[list[TranscriptSegment], Path]
            Segments + path to the canonical ``source_transcript.json``.
        """
        ...
