"""
processors/premiere/pipeline.py — One URL to a Premiere-ready timeline.

Ties the existing judgment layer to the Premiere handoff and runs the two
halves concurrently, because they need entirely different things:

    analysis   subtitles + chat + audio signals -> scored moments
    media      the full master VOD the timeline will reference

The master takes many minutes for a long VOD while analysis only needs the
audio, so waiting for the video before starting analysis would idle the
whole pipeline. Both run together and meet at FCPXML generation.

Nothing here re-encodes or cuts video: the output is one master file plus
an XML timeline that points at the good parts of it.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from models.clip import Clip, SignalEvent

from ..clip_finder import ClipFinder
from ..clip_finder import scoring
from .fcpxml import write_fcpxml
from .source import MasterSource, MediaInfo, probe_media

LogFn = Callable[[str], None]


@dataclass
class CompilationResult:
    """Everything a compilation run produced, successful or not."""

    clips: list[Clip] = field(default_factory=list)
    master: Path | None = None
    fcpxml: Path | None = None
    manifest: Path | None = None
    media: MediaInfo | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.clips) and self.fcpxml is not None

    @property
    def total_seconds(self) -> float:
        return sum(c.duration for c in self.clips)


async def build_compilation(
    *,
    url: str,
    output_dir: Path,
    api_keys: Sequence[str],
    instructions: str = "",
    lang: str = "ja",
    start_offset: float = 0.0,
    model: str = "gemini",
    mode: str = "single-shot",
    threshold: float | None = None,
    project_name: str = "",
    enable_chat: bool = True,
    enable_audio: bool = True,
    download_master: bool = True,
    finder: ClipFinder | None = None,
    log_fn: LogFn | None = None,
) -> CompilationResult:
    """Analyse ``url`` and emit a Premiere timeline of its best moments.

    Returns a :class:`CompilationResult`. Failure is reported through
    ``errors`` rather than raised, so a partial run (moments found but the
    master download failed) still hands back the moment list.
    """
    # Resolve up front so every path this run reports is absolute. Premiere
    # runs with its own working directory, so a relative path reaches it as
    # "output/compilation/..." and resolves to nothing — the import and the
    # reveal-folder action both fail with a confusing "not found".
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    result = CompilationResult()

    def log(message: str) -> None:
        if log_fn:
            log_fn(message)

    import config as _config

    cf = finder or ClipFinder(
        cookies_file=getattr(_config, "YTDLP_COOKIES_FILE", "") or "",
        cookies_browser=getattr(_config, "YTDLP_COOKIES_BROWSER", "") or "",
        gemini_model=getattr(_config, "CLIP_FINDER_GEMINI_MODEL", "gemini-3.5-flash"),
    )
    source = MasterSource(
        cookies_file=getattr(_config, "YTDLP_COOKIES_FILE", "") or "",
        cookies_browser=getattr(_config, "YTDLP_COOKIES_BROWSER", "") or "",
    )

    started = time.monotonic()
    log("Compilation: analysing moments and downloading master in parallel...")

    async def _media() -> Path | None:
        if not download_master:
            return None
        return await source.download_master(
            url=url, output_dir=output_dir, log_fn=log_fn,
        )

    async def _analysis() -> list[Clip]:
        return await _find_moments(
            cf=cf,
            url=url,
            output_dir=output_dir,
            api_keys=list(api_keys),
            instructions=instructions,
            lang=lang,
            start_offset=start_offset,
            model=model,
            mode=mode,
            threshold=threshold,
            enable_chat=enable_chat,
            enable_audio=enable_audio,
            log=log,
        )

    media_task = asyncio.create_task(_media())
    analysis_task = asyncio.create_task(_analysis())
    master_path, clips = await asyncio.gather(
        media_task, analysis_task, return_exceptions=True,
    )

    if isinstance(master_path, BaseException):
        result.errors.append(f"master download failed: {master_path}")
        master_path = None
    if isinstance(clips, BaseException):
        result.errors.append(f"analysis failed: {clips}")
        clips = []

    result.master = master_path
    result.clips = list(clips or [])
    elapsed = time.monotonic() - started
    log(
        f"Compilation: {len(result.clips)} moment(s), "
        f"{result.total_seconds / 60:.1f} min of material, in {elapsed:.0f}s"
    )

    if not result.clips:
        result.errors.append("no moments cleared the quality bar")
        return result
    if master_path is None:
        result.errors.append(
            "master video unavailable — the timeline needs it to link media"
        )
        return result

    result.media = probe_media(master_path, log_fn=log_fn)
    name = project_name.strip() or f"Compilation {time.strftime('%Y-%m-%d %H%M')}"
    result.fcpxml = write_fcpxml(
        result.clips,
        result.media,
        output_dir / "compilation.xml",
        sequence_name=name,
        source_url=url,
    )
    result.manifest = _write_manifest(result, output_dir, url=url, name=name)
    log(f"Compilation: timeline written to {result.fcpxml.name} — import it in Premiere")
    return result


async def _find_moments(
    *,
    cf: ClipFinder,
    url: str,
    output_dir: Path,
    api_keys: list[str],
    instructions: str,
    lang: str,
    start_offset: float,
    model: str,
    mode: str,
    threshold: float | None,
    enable_chat: bool,
    enable_audio: bool,
    log: LogFn,
) -> list[Clip]:
    """Subtitles + signals -> scored moments above the quality bar."""
    transcript = await cf.extract_subtitles(url, output_dir, lang=lang, log_fn=log)
    if not transcript:
        raise RuntimeError(
            f"no {lang} subtitles available — clip finding needs a transcript"
        )
    log(f"Transcript: {len(transcript)} segments")

    if start_offset > 0:
        before = len(transcript)
        transcript = cf.filter_transcript_by_offset(transcript, start_offset)
        log(f"Start offset {start_offset:.0f}s: {before} → {len(transcript)} segments")
        if not transcript:
            raise RuntimeError(
                f"no transcript left after the {start_offset:.0f}s start offset"
            )

    signals: list[SignalEvent] = []
    if enable_chat or enable_audio:
        signals = list(
            await cf.extract_signals(
                url,
                output_dir,
                log_fn=log,
                enable_audio=enable_audio,
                enable_chat=enable_chat,
                enable_visual=False,
            )
        )
    log(f"Signals: {len(signals)} event(s)")

    return await cf.find_clips(
        transcript=transcript,
        instructions=instructions,
        api_keys=api_keys,
        mode=mode,
        signals=signals,
        log_fn=log,
        model=model,
        clip_format=scoring.COMPILATION,
        threshold=threshold,
    )


def _write_manifest(
    result: CompilationResult,
    output_dir: Path,
    *,
    url: str,
    name: str,
) -> Path:
    """Machine-readable record of the run, beside the timeline.

    The source URL and title are recorded here as well as in the XML so the
    attribution a VTuber clip channel owes its source survives even if the
    timeline is re-exported.
    """
    payload = {
        "project": name,
        "source_url": url,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "master": str(result.master) if result.master else None,
        "fcpxml": str(result.fcpxml) if result.fcpxml else None,
        "fps": float(result.media.fps) if result.media else None,
        "moment_count": len(result.clips),
        "total_seconds": round(result.total_seconds, 2),
        "moments": [c.to_dict() for c in result.clips],
    }
    path = output_dir / "compilation_manifest.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return path


__all__ = ["build_compilation", "CompilationResult"]
