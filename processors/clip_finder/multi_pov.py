"""
processors/clip_finder/multi_pov.py — Workspace 05 (Multi POV) orchestrator.

MultiPOVFinder orchestrates N parallel Clip Finder pipelines (one per POV
Source) followed by a single LLM cross-matching pass.

Public API:

    MultiPOVFinder(...).find_pov_groups(
        sources,          # list[SourceInput]
        *,
        instructions,
        api_keys,
        mode,
        scoring_profile,
        model,
        enable_audio_signals,
        enable_chat_signals,
        log_fn,
        progress_fn,
    ) -> tuple[list[POVGroup], list[tuple[int, Clip]], list[SourceResult]]

Design:
  - Per-source pipelines run in parallel via asyncio.gather (Grill #6).
  - ClipFinder (Workspace 02 orchestrator) is instantiated once per source
    and reused intact — no modifications to Clip Finder code (Grill #8).
  - Cross-matching runs after ALL sources complete (Grill #4).
  - Edge case A2: a failed source is skipped gracefully; the job fails only
    if ALL sources fail.
  - Edge case B2: enforced inside cross_matcher._validate_groups.
  - Edge case C1: if no groups are matched, all clips are returned as
    unmatched. The caller (web route) renders the warning banner.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from models.clip import Clip, SignalEvent
from models.pov_group import POVGroup

from .clip_selection import ClipFinderError
from .cross_matcher import SourceMeta, match_clips_across_sources
from .gemini_client import GeminiClient
from .nine_router_client import NineRouterClient
from .orchestrator import ClipFinder
from .transcript import Segment

LogFn = Callable[[str], None]
ProgressFn = Callable[[int, float, str], None]  # (source_idx, pct, sub_phase)


# ─── Input / Result types ─────────────────────────────────────────────────────


@dataclass
class SourceInput:
    """Input descriptor for one POV Source."""
    url: str
    label: str = ""             # user-provided label; fallback to video_title
    start_offset: float = 0.0
    source_idx: int = 0         # position in the sources list


@dataclass
class SourceResult:
    """Per-source pipeline result (parallel to SourceInput)."""
    source_idx: int
    url: str
    label: str = ""
    video_title: str | None = None
    status: str = "pending"     # pending / extracting / analyzing / done / failed
    sub_phase: str = ""
    progress_pct: float = 0.0
    clips: list[Clip] = field(default_factory=list)
    transcript: list[dict] = field(default_factory=list)
    signals_summary: dict = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "source_idx": self.source_idx,
            "url": self.url,
            "label": self.label,
            "video_title": self.video_title,
            "status": self.status,
            "sub_phase": self.sub_phase,
            "progress_pct": round(self.progress_pct, 1),
            "clips_found": len(self.clips),
            "clips": [c.to_dict() for c in self.clips],
            "signals_summary": self.signals_summary,
            "error": self.error,
        }


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _build_llm_client(model: str, api_keys: list[str], gemini_model: str):
    """Mirror of ClipFinder._build_llm_client — reused for the matching call."""
    import config as _config

    normalised = (model or "gemini").strip().lower()
    if normalised in ("kiro-opus", "kiro-opus-4.7", "opus-4.7", "opus"):
        kiro_model = getattr(
            _config, "CLIP_FINDER_KIRO_OPUS_MODEL",
            "kr/claude-opus-4.7-thinking-agentic",
        )
        return NineRouterClient(model=kiro_model)

    if normalised in ("kiro-sonnet", "kiro-sonnet-4.6", "sonnet-4.6", "sonnet"):
        kiro_model = getattr(
            _config, "CLIP_FINDER_KIRO_SONNET_MODEL",
            "kr/claude-sonnet-4.6-thinking-agentic",
        )
        return NineRouterClient(model=kiro_model)

    if normalised in ("kiro-auto", "auto", "kiro"):
        kiro_model = getattr(_config, "CLIP_FINDER_KIRO_AUTO_MODEL", "kr/auto")
        return NineRouterClient(model=kiro_model)

    if normalised in ("codex-gpt-5.5", "gpt-5.5", "cx/gpt-5.5", "codex-55"):
        cx_model = getattr(_config, "CLIP_FINDER_CODEX_GPT55_MODEL", "cx/gpt-5.5")
        return NineRouterClient(model=cx_model)

    if normalised in ("codex-gpt-5.4", "gpt-5.4", "cx/gpt-5.4", "codex-54"):
        cx_model = getattr(_config, "CLIP_FINDER_CODEX_GPT54_MODEL", "cx/gpt-5.4")
        return NineRouterClient(model=cx_model)

    fallback_models = list(getattr(_config, "CLIP_FINDER_GEMINI_FALLBACK_MODELS", []))
    return GeminiClient(api_keys, model=gemini_model, fallback_models=fallback_models)


def _source_log_prefix(src: SourceInput) -> str:
    label = src.label or f"Source {src.source_idx}"
    return f"[Source {src.source_idx}: {label}]"


# ─── Main orchestrator class ──────────────────────────────────────────────────


class MultiPOVFinder:
    """Orchestrates the Multi POV pipeline.

    One instance is created per job.  It builds a :class:`ClipFinder`
    per source, runs them in parallel, then calls the cross-matcher.

    Parameters mirror those of :class:`.orchestrator.ClipFinder`.
    """

    def __init__(
        self,
        cookies_file: str = "",
        cookies_browser: str = "",
        gemini_model: str = "gemini-3.5-flash",
        cache_dir: Path | None = None,
        ffmpeg_path: str = "ffmpeg",
    ):
        self._cookies_file = cookies_file
        self._cookies_browser = cookies_browser
        self._gemini_model = gemini_model
        self._cache_dir = cache_dir
        self._ffmpeg_path = ffmpeg_path

    def _make_clip_finder(self) -> ClipFinder:
        """Construct a fresh ClipFinder instance (one per source)."""
        return ClipFinder(
            cookies_file=self._cookies_file,
            cookies_browser=self._cookies_browser,
            gemini_model=self._gemini_model,
            cache_dir=self._cache_dir,
            ffmpeg_path=self._ffmpeg_path,
        )

    async def _run_one_source(
        self,
        src: SourceInput,
        result: SourceResult,
        job_dir: Path,
        *,
        instructions: str,
        api_keys: list[str],
        mode: str,
        scoring_profile: str,
        model: str,
        enable_audio_signals: bool,
        enable_chat_signals: bool,
        log_fn: LogFn | None,
        progress_fn: ProgressFn | None,
    ) -> None:
        """Run the full Clip Finder pipeline for one source.

        Mutates ``result`` in-place. Never raises — failures are captured
        in ``result.status = "failed"`` so the parent gather continues.
        """
        prefix = _source_log_prefix(src)
        source_dir = job_dir / f"source_{src.source_idx}"
        source_dir.mkdir(parents=True, exist_ok=True)

        def log(msg: str) -> None:
            if log_fn:
                log_fn(f"{prefix} {msg}")

        def set_progress(pct: float, sub_phase: str) -> None:
            result.progress_pct = pct
            result.sub_phase = sub_phase
            if progress_fn:
                progress_fn(src.source_idx, pct, sub_phase)

        cf = self._make_clip_finder()

        try:
            # Step 1: Transcript
            result.status = "extracting"
            set_progress(5.0, "Extracting transcript")
            log("Step 1/4: Extracting transcript...")

            transcript = await cf.extract_subtitles(
                url=src.url,
                output_dir=source_dir / "subs",
                lang="ja",
                log_fn=log,
            )

            if not transcript:
                result.status = "failed"
                result.error = "No subtitles found for this URL."
                log("No subtitles found — skipping this source.")
                return

            # Apply start offset
            if src.start_offset > 0:
                transcript = cf.filter_transcript_by_offset(
                    transcript, src.start_offset
                )
                if not transcript:
                    result.status = "failed"
                    result.error = f"No transcript after {src.start_offset}s offset."
                    log(f"No segments remain after {src.start_offset}s offset.")
                    return

            result.transcript = list(transcript)
            log(f"Transcript extracted: {len(transcript)} segments")
            set_progress(20.0, "Transcript extracted")

            # Step 2: Signals
            set_progress(30.0, "Extracting signals")
            log("Step 2/4: Extracting multimodal signals...")

            signals = await cf.extract_signals(
                url=src.url,
                output_dir=source_dir / "signals",
                log_fn=log,
                enable_audio=enable_audio_signals,
                enable_chat=enable_chat_signals,
            )

            if src.start_offset > 0 and signals:
                signals = [s for s in signals if s.end > src.start_offset]

            from collections import Counter
            kinds = Counter(s.kind.value for s in signals)
            result.signals_summary = dict(kinds)
            set_progress(50.0, "Signals extracted")

            # Step 3: AI detection
            set_progress(55.0, "AI detection")
            log(f"Step 3/4: AI detection (mode={mode}, model={model})...")

            clips = await cf.find_clips(
                transcript=transcript,
                instructions=instructions,
                api_keys=api_keys,
                mode=mode,
                signals=signals,
                log_fn=log,
                max_count=12 if mode == "multi-stage" else None,
                scoring_profile=scoring_profile,
                model=model,
            )

            log(f"Detection complete: {len(clips)} clip(s) found")
            set_progress(90.0, "Scoring & refinement")

            result.clips = clips
            result.status = "done"
            result.progress_pct = 100.0
            result.sub_phase = "Done"
            log(f"Step 4/4: Complete — {len(clips)} clip(s) ready for matching")

        except (ClipFinderError, Exception) as exc:  # noqa: BLE001 — isolate per source
            result.status = "failed"
            result.error = str(exc)
            log(f"Source failed: {exc}")

    async def find_pov_groups(
        self,
        sources: Sequence[SourceInput],
        *,
        instructions: str,
        api_keys: list[str],
        mode: str = "single-shot",
        scoring_profile: str = "vtuber",
        model: str = "gemini",
        enable_audio_signals: bool = True,
        enable_chat_signals: bool = True,
        job_dir: Path,
        log_fn: LogFn | None = None,
        progress_fn: ProgressFn | None = None,
    ) -> tuple[list[POVGroup], list[tuple[int, Clip]], list[SourceResult]]:
        """Run the full Multi POV pipeline.

        Phase 1: Run Clip Finder pipeline for each source in parallel.
        Phase 2: Cross-match clips across sources into POVGroups.

        Parameters
        ----------
        sources:
            List of SourceInput descriptors (2–5).
        instructions:
            User's search instructions.
        api_keys:
            Gemini API key(s) — shared across all source pipelines.
        mode, scoring_profile, model, enable_audio_signals, enable_chat_signals:
            Passed through to each per-source ClipFinder.find_clips() call.
        job_dir:
            Root directory for this job's output files.
        log_fn:
            Callback for log lines (prefixed per source).
        progress_fn:
            Callback for per-source progress updates.

        Returns
        -------
        (pov_groups, unmatched, source_results)
        """
        def log(msg: str) -> None:
            if log_fn:
                log_fn(msg)

        # Initialise per-source result objects
        source_results: list[SourceResult] = [
            SourceResult(source_idx=src.source_idx, url=src.url, label=src.label)
            for src in sources
        ]

        # ── Phase 1: Parallel per-source Clip Finder ──────────────────────
        log(f"Starting parallel extraction for {len(sources)} source(s)...")

        tasks = [
            self._run_one_source(
                src,
                source_results[i],
                job_dir,
                instructions=instructions,
                api_keys=api_keys,
                mode=mode,
                scoring_profile=scoring_profile,
                model=model,
                enable_audio_signals=enable_audio_signals,
                enable_chat_signals=enable_chat_signals,
                log_fn=log_fn,
                progress_fn=progress_fn,
            )
            for i, src in enumerate(sources)
        ]
        await asyncio.gather(*tasks)

        # Edge case A2: fail the job only if ALL sources failed
        successful = [r for r in source_results if r.status == "done"]
        failed = [r for r in source_results if r.status == "failed"]

        if not successful:
            log(f"All {len(sources)} sources failed — cannot proceed to matching.")
            raise ClipFinderError(
                f"All {len(sources)} source(s) failed to extract transcripts. "
                "Check that each URL has subtitles available."
            )

        if failed:
            log(
                f"{len(failed)} source(s) failed and will be skipped: "
                + ", ".join(r.url for r in failed)
            )

        # Retrieve video titles from transcripts (yt-dlp may embed them)
        for res in source_results:
            if res.video_title is None and res.transcript:
                # Title is not in the transcript structure — leave as None
                pass

        # ── Phase 2: LLM Cross-Matching ──────────────────────────────────
        source_metas: list[SourceMeta] = [
            {
                "source_idx": r.source_idx,
                "url": r.url,
                "label": r.label,
                "video_title": r.video_title,
            }
            for r in successful
        ]
        source_clip_lists: list[list[Clip]] = [r.clips for r in successful]
        source_transcripts: list[list[dict]] = [r.transcript for r in successful]

        total_clips = sum(len(c) for c in source_clip_lists)
        if total_clips == 0:
            log("No clips found across any source — nothing to match.")
            return [], [], source_results

        log(
            f"All sources analyzed. Starting cross-matching "
            f"({total_clips} clips across {len(successful)} sources)..."
        )

        llm_client = _build_llm_client(model, api_keys, self._gemini_model)

        pov_groups, unmatched = await match_clips_across_sources(
            source_metas,
            source_clip_lists,
            source_transcripts,
            instructions=instructions,
            llm_client=llm_client,
            log_fn=log_fn,
        )

        multi_count = sum(1 for g in pov_groups if g.is_multi_pov)
        log(
            f"Cross-matching complete: {multi_count} multi-POV group(s), "
            f"{len(pov_groups) - multi_count} single-source group(s), "
            f"{len(unmatched)} unmatched clip(s)."
        )

        return pov_groups, unmatched, source_results


__all__ = ["MultiPOVFinder", "SourceInput", "SourceResult"]
