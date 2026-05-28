"""
processors/clip_finder/scoring.py — Two-tier scoring of clip candidates.

Stage 1 (deterministic): use the multimodal SignalEvent stream to compute
  - audio_peak_db          (max peak inside the clip range)
  - chat_spike_ratio        (max chat-spike intensity inside the clip range)
  - duration_fit            (10 = within target, decays toward edges)

Stage 2 (LLM): rate each candidate on five qualitative dimensions
  retention_hook / emotional_intensity / completeness / replayability /
  shorts_friendly. One Gemini call rates ALL candidates in batch.

Output: list[Clip] (candidates promoted to fully-scored Clip objects).
"""

from __future__ import annotations

from typing import Callable, Sequence

from models.clip import (
    Clip,
    ClipCandidate,
    ClipScore,
    SignalEvent,
    SignalKind,
)

from . import prompts
from .clip_selection import parse_candidates_json  # for response shape parity
from .gemini_client import GeminiClient
from .transcript import Segment, condense_for_prompt

LogFn = Callable[[str], None]


class ClipScorer:
    """Combines deterministic features + LLM rubric into ClipScore."""

    def __init__(self, client: GeminiClient | None):
        self._client = client

    async def score(
        self,
        *,
        candidates: Sequence[ClipCandidate],
        transcript: Sequence[Segment],
        instructions: str,
        signals: Sequence[SignalEvent] = (),
        min_clip: float = 15.0,
        max_clip: float = 120.0,
        log_fn: LogFn | None = None,
    ) -> list[Clip]:
        """Return Clip instances with a populated `score` field."""
        if not candidates:
            return []

        det_scores = [
            self._deterministic_features(c, signals, min_clip, max_clip)
            for c in candidates
        ]
        llm_scores = await self._llm_rubric(
            candidates, transcript, instructions, signals, log_fn
        )

        clips: list[Clip] = []
        for cand, det, llm in zip(candidates, det_scores, llm_scores):
            score = ClipScore(
                retention_hook=llm.get("retention_hook", 0.0),
                emotional_intensity=llm.get("emotional_intensity", 0.0),
                completeness=llm.get("completeness", 0.0),
                replayability=llm.get("replayability", 0.0),
                shorts_friendly=llm.get("shorts_friendly", 0.0),
                audio_peak_db=det["audio_peak_db"],
                chat_spike_ratio=det["chat_spike_ratio"],
                duration_fit=det["duration_fit"],
                coincidence_bonus=det.get("coincidence_bonus", 0.0),
            )
            overlapping_signals = self._signals_in_range(cand, signals)
            clip = Clip.from_candidate(cand, score=score)
            clip.signals = overlapping_signals
            # Punchline (#7): clamp into [0, duration] so a hallucinated
            # offset can never push downstream consumers off the end of
            # the Moment. None means "no opinion" — kept as-is.
            raw_punchline = llm.get("punchline_seconds_from_start")
            if raw_punchline is not None:
                try:
                    p = float(raw_punchline)
                    p = max(0.0, min(cand.duration, p))
                    clip.punchline_offset = round(p, 3)
                except (TypeError, ValueError):
                    pass
            clips.append(clip)

        return clips

    # ── Stage 1: deterministic features ──────────────────────────────────

    @staticmethod
    def _signals_in_range(
        candidate: ClipCandidate, signals: Sequence[SignalEvent]
    ) -> list[SignalEvent]:
        return [
            s for s in signals
            if s.end >= candidate.start and s.start <= candidate.end
        ]

    @staticmethod
    def _deterministic_features(
        candidate: ClipCandidate,
        signals: Sequence[SignalEvent],
        min_clip: float,
        max_clip: float,
    ) -> dict[str, float]:
        peaks = [
            s for s in signals
            if s.kind == SignalKind.AUDIO_PEAK
            and s.end >= candidate.start
            and s.start <= candidate.end
        ]
        chat_spikes = [
            s for s in signals
            if s.kind in (
                SignalKind.CHAT_SPIKE,
                SignalKind.CHAT_EMOTE_STORM,
                SignalKind.CHAT_SUPERCHAT,
            )
            and s.end >= candidate.start
            and s.start <= candidate.end
        ]

        # peak_db: read from label "+X dB above baseline" if present
        max_peak_db = 0.0
        for p in peaks:
            label = p.label or ""
            try:
                # label e.g. "+18.5 dB above baseline"
                num = label.split()[0].lstrip("+")
                max_peak_db = max(max_peak_db, float(num))
            except (ValueError, IndexError):
                # Fallback to intensity * 20
                max_peak_db = max(max_peak_db, p.intensity * 20.0)

        max_chat_ratio = 0.0
        for s in chat_spikes:
            # spike label e.g. "chat 4.5x baseline"
            label = s.label or ""
            try:
                if "x baseline" in label:
                    num = label.replace("chat", "").split("x")[0].strip()
                    max_chat_ratio = max(max_chat_ratio, float(num))
                else:
                    max_chat_ratio = max(max_chat_ratio, s.intensity * 5.0)
            except (ValueError, IndexError):
                max_chat_ratio = max(max_chat_ratio, s.intensity * 5.0)

        # duration_fit: 10 inside [min_clip, max_clip], decays linearly
        dur = candidate.duration
        if min_clip <= dur <= max_clip:
            duration_fit = 10.0
        else:
            target = (min_clip + max_clip) / 2.0
            spread = (max_clip - min_clip) / 2.0 + 1.0
            duration_fit = max(0.0, 10.0 - abs(dur - target) / spread * 5.0)

        # Co-occurrence bonus — audio peak AND chat spike inside the
        # same range is the highest-precision predictor per chat_signals.
        # We compute the temporal overlap (in seconds) between the
        # strongest peak and any chat-class spike; bigger overlap = more
        # confident the moment is clip-worthy. Capped at 10.0 so it
        # never dominates the LLM rubric. See May-28 audit "#6".
        coincidence_bonus = 0.0
        if peaks and chat_spikes:
            best_overlap = 0.0
            for p in peaks:
                for s in chat_spikes:
                    ov_start = max(p.start, s.start)
                    ov_end = min(p.end, s.end)
                    if ov_end > ov_start:
                        best_overlap = max(best_overlap, ov_end - ov_start)
            # 0s overlap → 0 bonus, 5s overlap → 10 bonus.
            coincidence_bonus = min(10.0, best_overlap * 2.0)

        return {
            "audio_peak_db": round(max_peak_db, 2),
            "chat_spike_ratio": round(max_chat_ratio, 2),
            "duration_fit": round(duration_fit, 2),
            "coincidence_bonus": round(coincidence_bonus, 2),
        }

    # ── Stage 2: LLM rubric ──────────────────────────────────────────────

    async def _llm_rubric(
        self,
        candidates: Sequence[ClipCandidate],
        transcript: Sequence[Segment],
        instructions: str,
        signals: Sequence[SignalEvent],
        log_fn: LogFn | None,
    ) -> list[dict[str, float]]:
        if not self._client:
            # No client available — neutral 5/10 across the board so total
            # score still functions on deterministic features.
            return [
                {
                    "retention_hook": 5.0,
                    "emotional_intensity": 5.0,
                    "completeness": 5.0,
                    "replayability": 5.0,
                    "shorts_friendly": 5.0,
                }
                for _ in candidates
            ]

        if log_fn:
            log_fn(f"Scoring {len(candidates)} candidate(s) via LLM rubric...")

        # Trim transcript for prompt budget — scoring sees a condensed view
        condensed = transcript
        if len(transcript) > 200:
            condensed = condense_for_prompt(list(transcript), max_segments=200)

        prompt = prompts.build_scoring_prompt(
            candidates=candidates,
            transcript=condensed,
            instructions=instructions,
            signals=signals,
        )

        try:
            text = await self._client.generate(
                prompt,
                max_output_tokens=8192,
                log_fn=log_fn,
                log_label="Scorer",
            )
        except Exception as exc:
            if log_fn:
                log_fn(f"Scorer LLM call failed: {exc}")
            return [self._neutral_score() for _ in candidates]

        ratings = self._parse_rubric(text, expected=len(candidates))
        if log_fn:
            log_fn(f"Scorer returned {sum(1 for r in ratings if r)} valid rating(s)")
        return [r or self._neutral_score() for r in ratings]

    @staticmethod
    def _neutral_score() -> dict[str, float]:
        return {
            "retention_hook": 5.0,
            "emotional_intensity": 5.0,
            "completeness": 5.0,
            "replayability": 5.0,
            "shorts_friendly": 5.0,
        }

    @staticmethod
    def _parse_rubric(text: str, expected: int) -> list[dict[str, float] | None]:
        """Parse `[{index, retention_hook, ...}]` keyed by 1-based index."""
        # We reuse the salvage logic but apply to RAW dict entries
        from .clip_selection import _extract_objects  # internal re-use

        objs = _extract_objects(text)
        out: list[dict[str, float] | None] = [None] * expected
        for obj in objs:
            if not isinstance(obj, dict):
                continue
            idx = obj.get("index")
            try:
                idx_int = int(idx) - 1
            except (TypeError, ValueError):
                continue
            if not (0 <= idx_int < expected):
                continue
            # ``punchline_seconds_from_start`` is optional — null when
            # the candidate has no single payoff beat. We pass it
            # through as-is (None or float) so the caller can tell the
            # difference between "no opinion" and "0 s".
            punchline = obj.get("punchline_seconds_from_start")
            try:
                punchline_val: float | None = (
                    float(punchline) if punchline is not None else None
                )
            except (TypeError, ValueError):
                punchline_val = None

            out[idx_int] = {
                "retention_hook": _coerce(obj.get("retention_hook"), 5.0),
                "emotional_intensity": _coerce(obj.get("emotional_intensity"), 5.0),
                "completeness": _coerce(obj.get("completeness"), 5.0),
                "replayability": _coerce(obj.get("replayability"), 5.0),
                "shorts_friendly": _coerce(obj.get("shorts_friendly"), 5.0),
                "punchline_seconds_from_start": punchline_val,
            }
        return out


def _coerce(value, default: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(10.0, v))


__all__ = ["ClipScorer"]
