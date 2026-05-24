"""
processors/clip_finder/detector.py — Single-call clip detection + recheck rescue.

Owns the Phase-1 LLM orchestration. Two public methods:

  - detect(transcript, instructions, signals=None) → list[ClipCandidate]
       The classic single-shot detection used when no Hunter mode is selected.

  - recheck(transcript, selected, instructions) → list[ClipCandidate]
       Second-pass rescue of segments not covered by `selected`.

Boundary-refinement, scoring, and diversification live in their own
modules — this one stays focused on prompt → LLM → parsed candidates.
"""

from __future__ import annotations

from typing import Callable, Sequence

from models.clip import ClipCandidate, HunterTag

from . import prompts
from .clip_selection import (
    deduplicate_candidates,
    parse_candidates_json,
)
from .gemini_client import GeminiClient
from .heuristics import is_vtuber_mode
from .transcript import Segment, condense_for_prompt, extract_discarded
from models.clip import SignalEvent

LogFn = Callable[[str], None]


class ClipDetector:
    """LLM-driven clip detector with a built-in rescue pass."""

    def __init__(self, client: GeminiClient):
        self._client = client

    # ── Phase 1: detect ──────────────────────────────────────────────────

    async def detect(
        self,
        *,
        transcript: Sequence[Segment],
        instructions: str,
        min_clip: float,
        max_clip: float,
        video_duration: float,
        signals: Sequence[SignalEvent] | None = None,
        log_fn: LogFn | None = None,
    ) -> list[ClipCandidate]:
        if log_fn:
            log_fn("Analysing transcript with Gemini AI...")

        working = self._condense(transcript, log_fn)

        prompt = prompts.build_detection_prompt(
            transcript=working,
            instructions=instructions or "",
            video_duration=video_duration,
            min_clip=min_clip,
            max_clip=max_clip,
            is_vtuber_mode=is_vtuber_mode(instructions),
            signals=signals,
        )

        text = await self._client.generate(
            prompt,
            log_fn=log_fn,
            log_label="Detect",
        )

        candidates = parse_candidates_json(
            text,
            min_duration=min_clip,
            max_duration=max_clip,
            hunter=HunterTag.GENERAL,
        )
        candidates = deduplicate_candidates(candidates)

        if log_fn:
            log_fn(f"Found {len(candidates)} clip(s) matching your instructions")
        return candidates

    # ── Phase 1.5: recheck rescue ────────────────────────────────────────

    async def recheck(
        self,
        *,
        transcript: Sequence[Segment],
        selected: Sequence[ClipCandidate],
        instructions: str,
        min_clip: float,
        max_clip: float,
        video_duration: float,
        log_fn: LogFn | None = None,
    ) -> list[ClipCandidate]:
        ranges = [(c.start, c.end) for c in selected]
        discarded = extract_discarded(transcript, ranges)
        if not discarded:
            if log_fn:
                log_fn("Recheck: no discarded segments to re-examine")
            return []

        discarded_duration = sum(seg["end"] - seg["start"] for seg in discarded)
        if discarded_duration < min_clip:
            if log_fn:
                log_fn(
                    f"Recheck: discarded content too short "
                    f"({discarded_duration:.1f}s), skipping"
                )
            return []

        if log_fn:
            log_fn(
                f"Recheck: re-examining {len(discarded)} discarded segments "
                f"({discarded_duration:.1f}s)..."
            )

        prompt = prompts.build_recheck_prompt(
            discarded=discarded,
            selected=selected,
            instructions=instructions,
            video_duration=video_duration,
            min_clip=min_clip,
            max_clip=max_clip,
            is_vtuber_mode=is_vtuber_mode(instructions),
        )

        try:
            text = await self._client.generate(
                prompt,
                max_output_tokens=32768,
                log_fn=log_fn,
                log_label="Recheck",
            )
        except Exception as exc:
            if log_fn:
                log_fn(f"Recheck error (non-fatal): {exc}")
            return []

        rescued = parse_candidates_json(
            text,
            min_duration=min_clip,
            max_duration=max_clip,
            hunter=HunterTag.GENERAL,
            rescued=True,
        )

        if log_fn:
            log_fn(f"Recheck: {len(rescued)} rescued clip(s) found")
        return rescued

    # ── helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _condense(
        transcript: Sequence[Segment], log_fn: LogFn | None
    ) -> list[Segment]:
        if len(transcript) <= 500:
            return list(transcript)
        condensed = condense_for_prompt(transcript, max_segments=500)
        if log_fn:
            log_fn(
                f"Condensed transcript: {len(transcript)} → {len(condensed)} segments"
            )
        return condensed


__all__ = ["ClipDetector"]
