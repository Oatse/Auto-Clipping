"""
web.services.all_in.stages.moments — Gemini moment detection adapter.

Thin wrapper around ``processors.clip_finder.ClipFinder`` that
collapses the three Clip Finder phases (subtitle extraction, signal
extraction, AI scoring) into one call returning a list of typed
``AllInClip`` rows ready to be written to the Job.

Why this is an adapter, not a fork: ``ClipFinder`` is already the
right shape — it's a class with stage-named methods.  We just want
a single context-free entry point that returns the data shape the
All In Job stores, so the runner doesn't have to know the Clip
Finder internals.

Public API:
    detect_moments(url, instructions, *, ...) -> MomentsResult

The future v1.1 service-layer refactor (ADR-0002) replaces this
file with a ``moments_service`` that the existing Clip Finder
workspace consumes too.
"""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..models import AllInClip, AllInClipStatus, DetectionMode

LogFn = Callable[[str], None]


# ─── Result type ─────────────────────────────────────────────────────────────

@dataclass
class MomentsResult:
    """What the moments stage hands back to the runner.

    The runner copies ``clips`` straight onto ``AllInJob.clips`` (so
    the UI starts seeing scored cards immediately, even before any
    rendering) and stores ``transcript`` + ``signals_summary`` for
    debug/inspection.
    """

    clips: list[AllInClip]
    transcript: list[dict] = field(default_factory=list)
    signals_summary: dict[str, int] = field(default_factory=dict)


class NoMomentsFoundError(RuntimeError):
    """Raised when Gemini returns zero scored clips.

    Distinct from a transcript / signal failure — this means the
    pipeline ran end-to-end but the user's instructions matched
    nothing.  The Job should reach a terminal state (``COMPLETED``
    with zero Clips, not ``FAILED``).
    """


class TranscriptUnavailableError(RuntimeError):
    """Raised when no subtitles can be extracted for the source URL."""


# ─── Public entry point ──────────────────────────────────────────────────────

async def detect_moments(
    *,
    url: str,
    instructions: str,
    job_dir: Path,
    analysis_lang: str = "en",
    mode: DetectionMode | str = DetectionMode.SINGLE_SHOT,
    enable_audio_signals: bool = True,
    enable_chat_signals: bool = True,
    start_offset: float = 0.0,
    max_clips: int = 12,
    gemini_keys: list[str],
    cookies_file: str = "",
    cookies_browser: str = "",
    gemini_model: str = "gemini-3.5-flash",
    cache_dir: Path | None = None,
    ffmpeg_path: str = "ffmpeg",
    log_fn: LogFn | None = None,
) -> MomentsResult:
    """Run transcript + signals + AI scoring in sequence.

    Returns a :class:`MomentsResult` whose ``clips`` are pre-populated
    with status :class:`AllInClipStatus.PENDING` — the runner will
    advance them through ``RENDERING`` → ``DONE``/``FAILED`` as the
    per-clip loop progresses.

    Raises :class:`TranscriptUnavailableError` when no subtitles can
    be found, and :class:`NoMomentsFoundError` when Gemini returns
    nothing.  Both are recoverable from the Job's perspective (the
    Job is reported as ``COMPLETED`` with zero or zero-after-filter
    Clips, not ``FAILED``).
    """
    # Lazy import keeps the all_in package importable in environments
    # where the heavier yt-dlp / clip_finder deps aren't present yet
    # (e.g. unit tests of the models / presets modules).
    from processors.clip_finder import ClipFinder

    cf = ClipFinder(
        cookies_file=cookies_file,
        cookies_browser=cookies_browser,
        gemini_model=gemini_model,
        cache_dir=cache_dir,
        ffmpeg_path=ffmpeg_path,
    )

    mode_str = mode.value if isinstance(mode, DetectionMode) else str(mode)

    # ── Step 1: transcript ─────────────────────────────────────────────
    if log_fn:
        log_fn(f"Step 1/3: Extracting transcript (lang={analysis_lang}, mode={mode_str})...")

    transcript = await cf.extract_subtitles(
        url=url,
        output_dir=job_dir / "subs",
        lang=analysis_lang,
        log_fn=log_fn,
    )
    if not transcript:
        raise TranscriptUnavailableError(
            "No subtitles found for this video. "
            "Tried auto-generated and manual subtitles in multiple languages."
        )

    # Apply start_offset (livestream waiting-screen skip).
    if start_offset > 0:
        original_count = len(transcript)
        transcript = cf.filter_transcript_by_offset(transcript, start_offset)
        if log_fn:
            log_fn(
                f"Applied start offset: {start_offset}s — "
                f"filtered {original_count} → {len(transcript)} segments"
            )
        if not transcript:
            raise TranscriptUnavailableError(
                f"No transcript segments remain after the {start_offset}s start offset."
            )

    if log_fn:
        log_fn(f"Transcript: {len(transcript)} segments")

    # ── Step 2: multimodal signals ─────────────────────────────────────
    if log_fn:
        log_fn("Step 2/3: Extracting multimodal signals (audio + chat)...")

    signals = await cf.extract_signals(
        url=url,
        output_dir=job_dir / "signals",
        log_fn=log_fn,
        enable_audio=enable_audio_signals,
        enable_chat=enable_chat_signals,
    )
    if start_offset > 0 and signals:
        signals = [s for s in signals if s.end > start_offset]

    signals_summary: dict[str, int] = dict(Counter(s.kind.value for s in signals))

    # ── Step 3: AI clip detection ─────────────────────────────────────
    if log_fn:
        log_fn(
            f"Step 3/3: Analyzing with Gemini AI "
            f"(mode={mode_str}, {len(gemini_keys)} key(s))..."
        )

    scored = await cf.find_clips(
        transcript=transcript,
        instructions=instructions,
        api_keys=gemini_keys,
        mode=mode_str,
        signals=signals,
        log_fn=log_fn,
        max_count=max_clips if mode_str == "multi-stage" else None,
    )

    if not scored:
        raise NoMomentsFoundError("Gemini returned zero scored moments.")

    # ── Build typed AllInClip rows ─────────────────────────────────────
    clips: list[AllInClip] = []
    # Enumerate AFTER sort-by-start so ``index`` reflects source-video
    # ordering, even though the UI defaults to score-desc display.
    # Source-time index is what the cut stage uses to label files
    # consistently across retries.
    for src_idx, c in enumerate(sorted(scored, key=lambda s: s.start)):
        d = c.to_dict()
        score_total = float(d.get("score", 0.0))
        if isinstance(d.get("score"), dict):  # nested {total, audio, chat, ...}
            score_total = float(d["score"].get("total", 0.0))

        clips.append(AllInClip(
            index=src_idx,
            start=float(d.get("start", 0.0)),
            end=float(d.get("end", 0.0)),
            title=str(d.get("title", f"Clip {src_idx + 1}")),
            reason=str(d.get("reason", "")),
            score=score_total,
            highlight_type=d.get("highlight_type"),
            hunter=d.get("hunter"),
            status=AllInClipStatus.PENDING,
        ))

    if log_fn:
        top_score = max((c.score for c in clips), default=0.0)
        log_fn(
            f"Analysis complete — {len(clips)} moment(s), "
            f"top score {top_score:.2f}/10"
        )

    # ``transcript`` is a list of Segment objects; the All In Job
    # stores them as plain dicts so they survive the JSON persistence
    # round-trip.  ``Segment.to_dict()`` is the canonical serializer.
    transcript_dicts: list[dict] = []
    for seg in transcript:
        if hasattr(seg, "to_dict"):
            transcript_dicts.append(seg.to_dict())
        elif isinstance(seg, dict):
            transcript_dicts.append(seg)
        # else: skip — unknown shape, won't deserialise cleanly

    return MomentsResult(
        clips=clips,
        transcript=transcript_dicts,
        signals_summary=signals_summary,
    )


__all__ = [
    "MomentsResult",
    "NoMomentsFoundError",
    "TranscriptUnavailableError",
    "detect_moments",
]
