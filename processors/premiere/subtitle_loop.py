"""
processors/premiere/subtitle_loop.py — Subtitle the timeline you just edited.

The last step of the workflow, and the reason it runs against Premiere rather
than the source VOD: by this point the timeline has been cut, re-ordered and
trimmed, so only Premiere knows what the finished video actually says. The
loop is:

    export the sequence's audio  ->  transcribe it  ->  bring captions back

Two consequences worth stating, because they drive the design:

* **Audio only, and only the finished cut.** Transcribing the two-hour source
  would bill for material that never airs; exporting the edited sequence keeps
  the speech-to-text bill proportional to the video actually published.
* **Timings are already timeline-relative.** The audio *is* the timeline, so
  the transcript needs no offset math to line up — which is exactly what makes
  this more reliable than transcribing the master and mapping ranges.

Premiere ships the preset this needs (``Waveform Audio 48kHz 16-bit.epr``), so
nothing has to be authored or bundled.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from models.transcript import TranscriptSegment

from .bridge_client import BridgeResponse, PremiereBridge, _escape

LogFn = Callable[[str], None]

# Premiere's own audio-only export preset. Searched for rather than hard-coded
# to one release, since the path carries the version year.
_PRESET_NAME = "Waveform Audio 48kHz 16-bit.epr"
_PRESET_ROOTS = (
    r"C:\Program Files\Adobe",
    r"/Applications",
)

# exportAsMediaDirect work-area constants.
ENTIRE_SEQUENCE = 0
IN_TO_OUT = 1


@dataclass
class SubtitleResult:
    """Outcome of one subtitle pass."""

    audio: Path | None = None
    srt: Path | None = None
    # One file per speaker, for stacking on separate caption tracks so
    # simultaneous speech keeps its own timing.
    speaker_srts: list[Path] = field(default_factory=list)
    segments: list[TranscriptSegment] = field(default_factory=list)
    # Diarisation ids in order of first appearance. Surfaced so the caller can
    # tell "one person talking" from "diarisation failed to separate them".
    speakers: list[str] = field(default_factory=list)
    imported: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.srt is not None and not self.errors


def _distinct_speakers(segments: Sequence[TranscriptSegment]) -> list[str]:
    order: list[str] = []
    for segment in segments:
        speaker = getattr(segment, "speaker", "") or ""
        if speaker and speaker not in order:
            order.append(speaker)
    return order


# ─── Preset discovery ────────────────────────────────────────────────────────


def find_audio_preset(explicit: str | None = None) -> Path | None:
    """Locate Premiere's WAV export preset.

    Checked in order: an explicit path, ``PREMIERE_AUDIO_PRESET``, then a
    search under the Adobe install root — the preset lives in a version-named
    folder, so searching survives a Premiere upgrade.
    """
    if explicit and Path(explicit).is_file():
        return Path(explicit)

    from_env = os.getenv("PREMIERE_AUDIO_PRESET")
    if from_env and Path(from_env).is_file():
        return Path(from_env)

    for root in _PRESET_ROOTS:
        base = Path(root)
        if not base.is_dir():
            continue
        try:
            for candidate in base.glob(f"**/systempresets/**/{_PRESET_NAME}"):
                if candidate.is_file():
                    return candidate
        except OSError:
            continue
    return None


# ─── Steps ───────────────────────────────────────────────────────────────────


def export_timeline_audio(
    bridge: PremiereBridge,
    out_path: Path,
    *,
    preset: Path | None = None,
    work_area: int = ENTIRE_SEQUENCE,
    timeout: float = 900.0,
    log_fn: LogFn | None = None,
) -> BridgeResponse:
    """Render the active sequence's audio to ``out_path``.

    Blocking inside Premiere, so the timeout is generous: a long timeline can
    take minutes even though audio-only export is comparatively quick.
    """
    resolved_preset = preset or find_audio_preset()
    if resolved_preset is None:
        return BridgeResponse.failed(
            f"Could not find {_PRESET_NAME}. Set PREMIERE_AUDIO_PRESET to its "
            "path if Premiere is installed somewhere unusual."
        )

    out_path = Path(out_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if log_fn:
        log_fn(f"Exporting timeline audio to {out_path.name}...")

    return bridge.execute(
        "var s = app.project.activeSequence;"
        "if (!s) { return '{\"success\":false,\"error\":\"No sequence is open\"}'; }"
        f'var res = s.exportAsMediaDirect("{_escape(str(out_path))}", '
        f'"{_escape(str(resolved_preset))}", {int(work_area)});'
        "var text = String(res);"
        # exportAsMediaDirect reports failure by returning a message rather
        # than throwing, so "No Error" is the only success value.
        "if (text !== 'No Error') { return '{\"success\":false,\"error\":\"' + "
        "text.replace(/\"/g, \"'\") + '\"}'; }"
        "return '{\"success\":true,\"data\":{\"exported\":true}}';",
        timeout=timeout,
    )


def import_captions(
    bridge: PremiereBridge,
    srt_path: Path,
    *,
    log_fn: LogFn | None = None,
) -> BridgeResponse:
    """Bring an SRT back into the open project as an editable caption item."""
    target = Path(srt_path).resolve()
    if log_fn:
        log_fn(f"Importing {target.name} into the project...")
    return bridge.execute(
        f'var f = new File("{_escape(str(target))}");'
        "if (!f.exists) { return '{\"success\":false,\"error\":\"Subtitle file "
        "not found\"}'; }"
        "var p = app.project;"
        "if (!p) { return '{\"success\":false,\"error\":\"No project open\"}'; }"
        f'var okc = p.importFiles(["{_escape(str(target))}"], true, p.rootItem, false);'
        "if (!okc) { return '{\"success\":false,\"error\":\"Premiere refused the "
        "subtitle import\"}'; }"
        "return '{\"success\":true,\"data\":{\"imported\":true}}';",
        timeout=120.0,
    )


# ─── SRT ─────────────────────────────────────────────────────────────────────


def format_timestamp(seconds: float) -> str:
    """SRT timestamp: ``HH:MM:SS,mmm``."""
    total_ms = max(0, int(round(seconds * 1000)))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def speaker_label(speaker_id: str, order: Sequence[str]) -> str:
    """Human label for a diarisation id, numbered by first appearance.

    ``SPEAKER_00`` is what the recogniser emits and what nobody wants to read
    in a caption. Numbering by order of appearance also keeps the labels
    stable and low, rather than echoing whatever ids diarisation happened to
    assign.
    """
    try:
        return f"Speaker {order.index(speaker_id) + 1}"
    except ValueError:
        return "Speaker 1"


def resolve_overlaps(
    segments: Sequence[TranscriptSegment],
    *,
    speaker_labels: bool = True,
) -> list[tuple[float, float, str]]:
    """Flatten segments into non-overlapping cues.

    SRT has no way to show two cues at once — the format is a sequence, and
    Premiere renders overlapping cues as one caption track fighting itself.
    But people talk over each other constantly: a five-way collab in this
    project's own output had 23 cross-speaker overlaps in 153 segments.

    So simultaneous speech is merged into a single cue carrying a line per
    speaker, which is what broadcast captions do:

        Speaker 1: no way
        Speaker 2: I told you

    The merged cue spans the union of the overlapping segments, so the
    individual in/out points are traded for both lines being readable at the
    time they were said. Same-speaker overlap is a diarisation artefact rather
    than simultaneous speech, so those are simply butted together instead.
    """
    ordered = sorted(
        (s for s in segments if (s.text or "").strip()),
        key=lambda s: (s.start, s.end),
    )
    if not ordered:
        return []

    order: list[str] = []
    for segment in ordered:
        speaker = getattr(segment, "speaker", "") or ""
        if speaker and speaker not in order:
            order.append(speaker)
    multi_speaker = speaker_labels and len(order) > 1

    cues: list[tuple[float, float, str]] = []
    group: list[TranscriptSegment] = []

    def flush() -> None:
        if not group:
            return
        start = min(s.start for s in group)
        end = max(max(s.end, s.start) for s in group)
        speakers_here = []
        for s in group:
            sp = getattr(s, "speaker", "") or ""
            if sp not in speakers_here:
                speakers_here.append(sp)

        if len(group) == 1 or len(speakers_here) < 2:
            # One voice: keep the lines as they are rather than gluing a
            # whole overlapping run into one long caption.
            for s in group:
                text = (s.text or "").strip()
                if text:
                    cues.append((s.start, max(s.end, s.start), text))
            return

        # Genuine simultaneous speech: one cue, one line per speaker, in the
        # order they started.
        lines: list[str] = []
        for sp in speakers_here:
            said = " ".join(
                (s.text or "").strip()
                for s in group
                if (getattr(s, "speaker", "") or "") == sp
            ).strip()
            if not said:
                continue
            lines.append(
                f"{speaker_label(sp, order)}: {said}" if multi_speaker else said
            )
        cues.append((start, end, "\n".join(lines)))

    for segment in ordered:
        if not group:
            group = [segment]
            continue
        group_end = max(max(s.end, s.start) for s in group)
        if segment.start < group_end - 1e-6:
            group.append(segment)          # overlaps the run so far
        else:
            flush()
            group = [segment]
    flush()

    # Merging can leave a cue running into the next one; trim rather than
    # emit overlapping cues, which is the problem being solved.
    cues.sort(key=lambda c: (c[0], c[1]))
    trimmed: list[tuple[float, float, str]] = []
    for index, (start, end, text) in enumerate(cues):
        if index + 1 < len(cues):
            end = min(end, cues[index + 1][0])
        if end <= start:
            end = start + 0.2              # keep a visible minimum
        trimmed.append((start, end, text))
    return trimmed


def build_srt(
    segments: Sequence[TranscriptSegment],
    *,
    speaker_labels: bool = True,
) -> str:
    """Render segments as SRT.

    Empty segments are skipped and cues are renumbered, because Premiere shows
    a blank caption for an empty cue rather than ignoring it.

    Speaker labels are added only when more than one speaker was actually
    detected. Prefixing every line of a solo stream with "Speaker 1" is pure
    noise, so the decision is made from the data rather than from a flag.
    """
    order: list[str] = []
    if speaker_labels:
        for segment in segments:
            speaker = getattr(segment, "speaker", "") or ""
            if speaker and speaker not in order:
                order.append(speaker)
    multi_speaker = speaker_labels and len(order) > 1

    cues = resolve_overlaps(segments, speaker_labels=speaker_labels)

    blocks: list[str] = []
    previous_speaker: str | None = None
    for index, (start, end, text) in enumerate(cues, start=1):
        if multi_speaker and "\n" not in text:
            # Single-voice cue: label it only when the speaker changes, since
            # repeating the name through one turn wastes caption width.
            # Merged cues already carry a label per line.
            speaker = _speaker_at(segments, start)
            if speaker and speaker != previous_speaker:
                text = f"{speaker_label(speaker, order)}: {text}"
            previous_speaker = speaker
        elif "\n" in text:
            previous_speaker = None        # a merged cue ends the run
        blocks.append(
            f"{index}\n{format_timestamp(start)} --> {format_timestamp(end)}\n{text}\n"
        )
    return "\n".join(blocks)


def _speaker_at(
    segments: Sequence[TranscriptSegment], start: float
) -> str:
    """Speaker of the segment beginning at ``start``."""
    for segment in segments:
        if abs(segment.start - start) < 1e-6:
            return getattr(segment, "speaker", "") or ""
    return ""


def write_srt(
    segments: Sequence[TranscriptSegment],
    path: Path,
    *,
    speaker_labels: bool = True,
) -> Path:
    """Write segments to ``path`` as SRT and return it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        build_srt(segments, speaker_labels=speaker_labels), encoding="utf-8",
    )
    return path


def write_per_speaker_srt(
    segments: Sequence[TranscriptSegment],
    output_dir: Path,
    *,
    stem: str = "timeline",
) -> list[Path]:
    """Write one SRT per speaker, for stacking on separate caption tracks.

    The merged single file keeps simultaneous speech readable on one track,
    but flattens it: both lines share the union of their timings. Premiere
    supports several caption tracks, and one file per speaker lets each keep
    its own exact in/out points and be styled and positioned independently —
    the closest thing to the per-speaker colours the burn-in renderer does.

    Returns an empty list for a single-speaker transcript, where separate
    tracks would add nothing.
    """
    order = _distinct_speakers(segments)
    if len(order) < 2:
        return []

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for number, speaker in enumerate(order, start=1):
        owned = [
            s for s in segments
            if (getattr(s, "speaker", "") or "") == speaker
            and (s.text or "").strip()
        ]
        if not owned:
            continue
        # No labels here: the file itself identifies the speaker, and the
        # track will be styled per speaker anyway.
        path = output_dir / f"{stem}.speaker{number}.srt"
        path.write_text(build_srt(owned, speaker_labels=False), encoding="utf-8")
        written.append(path)
    return written


# ─── The loop ────────────────────────────────────────────────────────────────


async def subtitle_timeline(
    *,
    output_dir: Path,
    bridge: PremiereBridge | None = None,
    language: str | None = None,
    speaker_detection: bool = True,
    num_speakers: int | None = None,
    keep_audio: bool = False,
    import_back: bool = True,
    translate_to: str | None = None,
    translator_backend: str | None = None,
    spicy_filter: bool = True,
    natural_caption: bool = True,
    speaker_labels: bool = True,
    per_speaker_tracks: bool = True,
    engine=None,
    translator=None,
    log_fn: LogFn | None = None,
) -> SubtitleResult:
    """Export the open sequence's audio, transcribe it, and return captions.

    Text processing deliberately mirrors the auto-subtitle pipeline rather
    than inventing its own: translate with word-level regrouping, then apply
    the natural caption style. That combination is the tuned one, and captions
    built any other way come out visibly more ragged — arbitrary recogniser
    chunks, trailing micro-punctuation, and segments that overstay on screen.

    ``translate_to`` captions a Japanese stream for an English audience, which
    is the normal case here rather than an extra. Timing survives; only the
    text changes.

    Failures are reported on the result rather than raised, so a run that
    transcribes successfully but cannot import still hands back the SRT.
    """
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    result = SubtitleResult()

    def log(message: str) -> None:
        if log_fn:
            log_fn(message)

    bridge = bridge or PremiereBridge(timeout=900.0, log_fn=log_fn)
    if not bridge.available():
        result.errors.append(bridge.unavailable_reason())
        return result

    audio_path = output_dir / "timeline_audio.wav"
    exported = export_timeline_audio(bridge, audio_path, log_fn=log_fn)
    if not exported.success:
        result.errors.append(f"audio export failed: {exported.error}")
        return result
    if not audio_path.is_file():
        result.errors.append("Premiere reported success but wrote no audio file")
        return result

    result.audio = audio_path
    size_mb = audio_path.stat().st_size / (1024 * 1024)
    log(f"Timeline audio exported ({size_mb:.0f} MB). Transcribing...")

    try:
        if engine is None:
            from processors.stt import ElevenLabsSttEngine

            engine = ElevenLabsSttEngine()
        segments, _ = await engine.transcribe(
            audio_path,
            output_dir,
            speaker_detection=speaker_detection,
            num_speakers=num_speakers,
            language_code=language,
        )
    except Exception as exc:  # noqa: BLE001 — surface, do not crash the job
        result.errors.append(f"transcription failed: {exc}")
        return result

    result.segments = list(segments)
    result.speakers = _distinct_speakers(result.segments)
    log(
        f"Transcribed {len(result.segments)} segment(s), "
        f"{len(result.speakers)} speaker(s) detected"
    )
    if speaker_detection and len(result.speakers) < 2 and num_speakers is None:
        # Diarisation frequently collapses a collab to one speaker unless it
        # is told how many to expect, and silently producing unlabelled
        # captions for a multi-person stream is the confusing outcome.
        log(
            "Only one speaker was separated. If this clip has several people "
            "talking, set the speaker count instead of leaving it on auto — "
            "the hint markedly improves separation."
        )

    if translate_to:
        try:
            if translator is None:
                from processors.translator import TranslatorProcessor

                translator = TranslatorProcessor(
                    target_language=translate_to,
                    backend=translator_backend,
                    spicy_filter=spicy_filter,
                )
            # regroup=True is what makes the captions read well: it re-cuts
            # them on word-level timings from the transcript instead of
            # keeping the recogniser's arbitrary chunks. This mirrors the
            # auto-subtitle pipeline exactly (web/services/pipeline_runner),
            # which is the tuned path — captions produced any other way come
            # out noticeably more ragged.
            translated, _ = await translator.translate(
                segments=result.segments,
                output_dir=output_dir,
                regroup=True,
            )
            result.segments = list(translated)
            log(f"Translated to {translate_to} with word-level regrouping")
        except Exception as exc:  # noqa: BLE001
            # Keep the source-language captions rather than losing the whole
            # run: they are still usable, and the transcription is already paid
            # for.
            result.errors.append(f"translation failed, keeping source text: {exc}")

    if natural_caption:
        # Second half of the tuned recipe: drop trailing micro-punctuation and
        # split over-long segments so no caption hangs on screen too long.
        # Idempotent, so running it after a failed translation is safe.
        try:
            from processors.timing import apply_natural_caption_style

            before = len(result.segments)
            result.segments = list(apply_natural_caption_style(result.segments))
            log(f"Caption styling: {before} → {len(result.segments)} segment(s)")
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"caption styling skipped: {exc}")

    # Recompute after regrouping: it splits at speaker changes and assigns each
    # new segment its majority speaker, so the set can shift.
    result.speakers = _distinct_speakers(result.segments)
    result.srt = write_srt(
        result.segments,
        output_dir / "timeline.srt",
        speaker_labels=speaker_labels,
    )
    if per_speaker_tracks:
        # Simultaneous speech survives as separate tracks here, with each
        # speaker keeping their exact timings instead of sharing a merged cue.
        result.speaker_srts = write_per_speaker_srt(result.segments, output_dir)
        if result.speaker_srts:
            log(
                f"Also wrote {len(result.speaker_srts)} per-speaker caption "
                "file(s) — put each on its own caption track to let voices "
                "overlap with their own timing."
            )

    if import_back:
        imported = import_captions(bridge, result.srt, log_fn=log_fn)
        result.imported = imported.success
        if not imported.success:
            # Not fatal: the SRT exists and can be imported by hand.
            result.errors.append(f"caption import failed: {imported.error}")
        else:
            # Bring the per-speaker files in as well, so the tracks are ready
            # to drop without going back out to the file system.
            for extra in result.speaker_srts:
                import_captions(bridge, extra, log_fn=log_fn)

    if not keep_audio:
        # A 13-minute timeline is ~150 MB of WAV; keeping it by default would
        # quietly fill the disk across runs.
        try:
            audio_path.unlink(missing_ok=True)
            result.audio = None
        except OSError:
            pass

    return result


__all__ = [
    "SubtitleResult",
    "subtitle_timeline",
    "find_audio_preset",
    "export_timeline_audio",
    "import_captions",
    "build_srt",
    "write_srt",
    "write_per_speaker_srt",
    "resolve_overlaps",
    "speaker_label",
    "format_timestamp",
    "ENTIRE_SEQUENCE",
    "IN_TO_OUT",
]
