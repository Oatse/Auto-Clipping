"""
processors/clip_finder/orchestrator.py — Top-level pipeline facade.

This is the public class the rest of the application talks to. It wires
together the deep modules and exposes only the operations the FastAPI
server needs:

  - find_clips(...)        Phase-1 detection (transcript → scored Clip[])
  - download_clips(...)    Phase-2 yt-dlp section download
  - slice_transcript(...)  Per-clip auto-sub re-timing for the renderer

The legacy single-shot detection mode (no Hunters, no Scorer) is still
available via `mode="single-shot"` so existing UI flows keep working
while the multi-stage pipeline is rolled out.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Sequence

from models.clip import Clip, ClipCandidate, SignalEvent

from . import boundary, selection
from .audio_signals import AudioSignalExtractor
from .cache import TranscriptCache
from .chat_signals import ChatSignalExtractor
from .clip_selection import ClipFinderError, deduplicate_clips
from .detector import ClipDetector
from .downloader import ClipDownloader
from .gemini_client import GeminiClient
from .heuristics import (
    fmt_duration,
    fmt_time,
    is_vtuber_mode,
    parse_duration_hints,
)
from .hunters import HunterRunner
from .scoring import ClipScorer
from .subtitle_source import SubtitleSource
from .transcript import (
    Segment,
    filter_by_offset,
    slice_for_clip,
)

LogFn = Callable[[str], None]


class ClipFinder:
    """Orchestrates transcript extraction, AI analysis, and clip download.

    Backwards-compatible entry point. The legacy single-shot path is the
    default; opt into the multi-stage pipeline with `mode="multi-stage"`
    on `find_clips`.
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
        self._cache = TranscriptCache(cache_dir) if cache_dir else None

        self._subs = SubtitleSource(cookies_file, cookies_browser)
        self._dl = ClipDownloader(cookies_file, cookies_browser)
        self._audio = AudioSignalExtractor(
            ffmpeg_path=ffmpeg_path,
            cookies_file=cookies_file,
            cookies_browser=cookies_browser,
        )
        self._chat = ChatSignalExtractor(cookies_file, cookies_browser)

    # ── Public: subtitle extraction ──────────────────────────────────────

    async def extract_subtitles(
        self,
        url: str,
        output_dir: Path,
        lang: str = "en",
        log_fn: LogFn | None = None,
        use_cache: bool = True,
    ) -> list[Segment] | None:
        if use_cache and self._cache:
            cached = self._cache.load_transcript(url)
            if cached:
                if log_fn:
                    log_fn(f"Using cached transcript ({len(cached)} segments)")
                return cached

        result = await self._subs.extract(url, output_dir, lang, log_fn=log_fn)
        if result is not None and use_cache and self._cache:
            self._cache.save_transcript(url, result)
        return result

    async def extract_signals(
        self,
        url: str,
        output_dir: Path,
        log_fn: LogFn | None = None,
        use_cache: bool = True,
        enable_audio: bool = True,
        enable_chat: bool = True,
    ) -> list[SignalEvent]:
        """Extract multimodal signals (audio peaks + silences + chat)."""
        if use_cache and self._cache:
            cached = self._cache.load_signals(url)
            if cached is not None:
                if log_fn:
                    log_fn(f"Using cached multimodal signals ({len(cached)} events)")
                return cached

        events: list[SignalEvent] = []

        if enable_chat:
            try:
                chat_events = await self._chat.extract(
                    url, output_dir / "chat", log_fn=log_fn
                )
                events.extend(chat_events)
            except Exception as exc:
                if log_fn:
                    log_fn(f"ChatSignals (non-fatal): {exc}")

        if enable_audio:
            try:
                audio_events = await self._audio.extract(
                    url, output_dir / "audio", log_fn=log_fn
                )
                events.extend(audio_events)
            except Exception as exc:
                if log_fn:
                    log_fn(f"AudioSignals (non-fatal): {exc}")

        events.sort(key=lambda e: e.start)
        if use_cache and self._cache:
            self._cache.save_signals(url, events)
        return events

    # ── Public: filter helpers (kept for backward compat) ────────────────

    @staticmethod
    def filter_transcript_by_offset(
        transcript: list[dict], start_offset: float
    ) -> list[dict]:
        return filter_by_offset(transcript, start_offset)

    @staticmethod
    def slice_transcript_for_clip(
        transcript: list[dict],
        clip_start: float,
        clip_end: float,
        padding: float = 0.5,
    ) -> list[dict]:
        return slice_for_clip(transcript, clip_start, clip_end, padding)

    # ── Public: find clips ───────────────────────────────────────────────

    async def find_clips(
        self,
        *,
        transcript: Sequence[Segment],
        instructions: str,
        api_keys: list[str],
        mode: str = "single-shot",          # "single-shot" | "multi-stage"
        signals: Sequence[SignalEvent] = (),
        log_fn: LogFn | None = None,
        max_count: int | None = None,
    ) -> list[Clip]:
        """Run clip detection. Returns scored, refined Clip objects.

        single-shot   : legacy path — one detection prompt + recheck pass.
        multi-stage   : Hunters → score → boundary refine → diversify select.
        """
        if not transcript:
            return []

        # Resolve fallback models from config so a deprecated primary model
        # (e.g. preview Gemini that was rotated out) doesn't break detection.
        try:
            import config as _config  # local import to avoid cycle at module load
            fallback_models = list(getattr(
                _config, "CLIP_FINDER_GEMINI_FALLBACK_MODELS", [],
            ))
        except Exception:  # noqa: BLE001 — config is optional in tests
            fallback_models = []

        client = GeminiClient(
            api_keys,
            model=self._gemini_model,
            fallback_models=fallback_models,
        )
        video_duration = max((seg["end"] for seg in transcript), default=0.0)
        min_clip, max_clip = parse_duration_hints(instructions or "", video_duration)

        if mode == "multi-stage":
            return await self._find_multi_stage(
                client=client,
                transcript=transcript,
                instructions=instructions,
                signals=signals,
                min_clip=min_clip,
                max_clip=max_clip,
                video_duration=video_duration,
                max_count=max_count,
                log_fn=log_fn,
            )

        return await self._find_single_shot(
            client=client,
            transcript=transcript,
            instructions=instructions,
            signals=signals,
            min_clip=min_clip,
            max_clip=max_clip,
            video_duration=video_duration,
            log_fn=log_fn,
        )

    # ── Detection paths ──────────────────────────────────────────────────

    async def _find_single_shot(
        self,
        *,
        client: GeminiClient,
        transcript: Sequence[Segment],
        instructions: str,
        signals: Sequence[SignalEvent],
        min_clip: float,
        max_clip: float,
        video_duration: float,
        log_fn: LogFn | None,
    ) -> list[Clip]:
        detector = ClipDetector(client)

        candidates = await detector.detect(
            transcript=transcript,
            instructions=instructions,
            min_clip=min_clip,
            max_clip=max_clip,
            video_duration=video_duration,
            signals=signals,
            log_fn=log_fn,
        )
        if not candidates:
            return []

        rescued = await detector.recheck(
            transcript=transcript,
            selected=candidates,
            instructions=instructions or "",
            min_clip=min_clip,
            max_clip=max_clip,
            video_duration=video_duration,
            log_fn=log_fn,
        )
        if rescued:
            candidates = candidates + rescued

        # Score (deterministic features only — single-shot mode keeps the
        # old behaviour: no LLM rubric call to save tokens)
        scorer = ClipScorer(client=None)
        clips = await scorer.score(
            candidates=candidates,
            transcript=transcript,
            instructions=instructions,
            signals=signals,
            min_clip=min_clip,
            max_clip=max_clip,
            log_fn=log_fn,
        )

        clips = boundary.refine_boundaries(clips, signals, min_duration=min_clip * 0.6)
        clips = deduplicate_clips(clips)
        return sorted(clips, key=lambda c: c.start)

    async def _find_multi_stage(
        self,
        *,
        client: GeminiClient,
        transcript: Sequence[Segment],
        instructions: str,
        signals: Sequence[SignalEvent],
        min_clip: float,
        max_clip: float,
        video_duration: float,
        max_count: int | None,
        log_fn: LogFn | None,
    ) -> list[Clip]:
        if log_fn:
            log_fn("Multi-stage pipeline: Hunters → Score → Refine → Select")

        hunters = HunterRunner(client)
        candidates = await hunters.run(
            transcript=transcript,
            instructions=instructions or "",
            min_clip=min_clip,
            max_clip=max_clip,
            video_duration=video_duration,
            signals=signals,
            log_fn=log_fn,
        )
        if not candidates:
            if log_fn:
                log_fn("Multi-stage: no candidates found, falling back to single-shot")
            return await self._find_single_shot(
                client=client,
                transcript=transcript,
                instructions=instructions,
                signals=signals,
                min_clip=min_clip,
                max_clip=max_clip,
                video_duration=video_duration,
                log_fn=log_fn,
            )

        # Score with full LLM rubric
        scorer = ClipScorer(client=client)
        clips = await scorer.score(
            candidates=candidates,
            transcript=transcript,
            instructions=instructions,
            signals=signals,
            min_clip=min_clip,
            max_clip=max_clip,
            log_fn=log_fn,
        )

        clips = boundary.refine_boundaries(
            clips, signals, min_duration=min_clip * 0.6
        )
        clips = deduplicate_clips(clips)

        # Diversified selection
        chosen = selection.select_top_clips(
            clips,
            max_count=max_count or 12,
        )
        if log_fn:
            log_fn(
                f"Multi-stage selected {len(chosen)} of {len(clips)} clip(s) "
                f"after diversification"
            )
        return chosen

    # ── Public: download ─────────────────────────────────────────────────

    async def download_clip_sections(
        self,
        url: str,
        clips: list[dict],
        output_dir: Path,
        log_fn: LogFn | None = None,
        index_offset: int = 0,
    ) -> list[Path]:
        return await self._dl.download(
            url=url,
            clips=clips,
            output_dir=output_dir,
            log_fn=log_fn,
            index_offset=index_offset,
        )

    # ── Misc helpers ─────────────────────────────────────────────────────

    @staticmethod
    def fmt_time(secs: float) -> str:
        return fmt_time(secs)

    @staticmethod
    def fmt_duration(secs: float) -> str:
        return fmt_duration(secs)


__all__ = ["ClipFinder", "ClipFinderError"]
