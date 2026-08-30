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
    segments: list[TranscriptSegment] = field(default_factory=list)
    imported: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.srt is not None and not self.errors


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


def build_srt(segments: Sequence[TranscriptSegment]) -> str:
    """Render segments as SRT.

    Empty segments are skipped and cues are renumbered, because Premiere shows
    a blank caption for an empty cue rather than ignoring it.
    """
    blocks: list[str] = []
    index = 0
    for segment in segments:
        text = (segment.text or "").strip()
        if not text:
            continue
        index += 1
        start = format_timestamp(segment.start)
        end = format_timestamp(max(segment.end, segment.start))
        blocks.append(f"{index}\n{start} --> {end}\n{text}\n")
    return "\n".join(blocks)


def write_srt(segments: Sequence[TranscriptSegment], path: Path) -> Path:
    """Write segments to ``path`` as SRT and return it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_srt(segments), encoding="utf-8")
    return path


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
    log(f"Transcribed {len(result.segments)} segment(s)")

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

    result.srt = write_srt(result.segments, output_dir / "timeline.srt")

    if import_back:
        imported = import_captions(bridge, result.srt, log_fn=log_fn)
        result.imported = imported.success
        if not imported.success:
            # Not fatal: the SRT exists and can be imported by hand.
            result.errors.append(f"caption import failed: {imported.error}")

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
    "format_timestamp",
    "ENTIRE_SEQUENCE",
    "IN_TO_OUT",
]
