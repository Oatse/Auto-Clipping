"""
processors/clip_finder/hunters.py — Single-aspect Hunter implementation.

Tier-3 / Pola A from the roadmap: instead of a single LLM call asked to
"find everything", run several focused calls each looking for ONE aspect
(scream / laughter / clutch / fail / wholesome / ...). Higher recall,
each call parameterisable, easy to extend.

Hunters can run sequentially (current default) or in parallel (configured
via `parallel=True` once Gemini concurrency budget allows).

Output: list[ClipCandidate] tagged by HunterTag.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Callable, Sequence

from models.clip import ClipCandidate, HunterTag, SignalEvent

from . import prompts
from .clip_selection import deduplicate_candidates, parse_candidates_json
from .gemini_client import GeminiClient
from .transcript import Segment, condense_for_prompt

LogFn = Callable[[str], None]


@dataclass
class HunterSpec:
    tag: HunterTag
    description: str


# ─── Built-in hunters ─────────────────────────────────────────────────────────

DEFAULT_HUNTERS: tuple[HunterSpec, ...] = (
    HunterSpec(
        HunterTag.SCREAM,
        "The speaker SCREAMS, yells loudly in panic, or yelps in surprise. "
        "Genuine vocal explosion — not just raised volume. Audio peaks help "
        "but the transcript text often contains 'AAAAH', 'WHAT', 'NO NO NO', "
        "or sudden short interjections. Look for moments where chat reacts "
        "with shock emotes too.",
    ),
    HunterSpec(
        HunterTag.LAUGHTER,
        "The speaker breaks into GENUINE belly-laugh — wheezing, can't speak, "
        "tears in eyes. Not chuckles, not 'haha that's funny'. The transcript "
        "often shows long laughter bursts ('hahahahaha'), gasping for air, "
        "and chat floods with LUL/KEKW/LMAO emote storms.",
    ),
    HunterSpec(
        HunterTag.RAGE,
        "Frustration peak — controller-throwing, profanity escalation, "
        "voice strain from anger, ragequit threats. Often follows a fail. "
        "Distinguish from playful complaints by intensity and duration.",
    ),
    HunterSpec(
        HunterTag.CLUTCH,
        "Speaker survives or wins against odds: low HP comeback, last-second "
        "save, perfect aim moment, solving a puzzle after long struggle. "
        "Energy goes from tense → release. Chat erupts in PogChamp / W in chat.",
    ),
    HunterSpec(
        HunterTag.FAIL,
        "Embarrassing, ironic, or karma-style failure. Speaker brags then "
        "immediately fails. Misclicks at worst possible moment. Sequences where "
        "the SETUP (overconfidence) is just as important as the punchline.",
    ),
    HunterSpec(
        HunterTag.WHOLESOME,
        "Touching, sweet, or vulnerable moments. Speaker shares personal story, "
        "thanks chat sincerely, gets emotional about a donation, has heartfelt "
        "exchange with another VTuber/streamer.",
    ),
    HunterSpec(
        HunterTag.META,
        "Breaking the fourth wall — addressing chat directly, inside jokes "
        "about previous streams, self-deprecating jokes about own brand, "
        "callbacks to old memes the community recognises.",
    ),
    HunterSpec(
        HunterTag.SCARED,
        "Genuine fear / jumpscare reaction in horror games. Voice cracks, "
        "physical recoil, possibly screaming (overlap with SCREAM is fine — "
        "scoring + dedup handles it later). Audio peak right after a quiet "
        "build-up is the signature pattern.",
    ),
)


# ─── HunterRunner ─────────────────────────────────────────────────────────────


class HunterRunner:
    """Runs a list of HunterSpec calls and collects candidates."""

    def __init__(self, client: GeminiClient):
        self._client = client

    async def run(
        self,
        *,
        transcript: Sequence[Segment],
        instructions: str,
        min_clip: float,
        max_clip: float,
        video_duration: float,
        signals: Sequence[SignalEvent] | None = None,
        hunters: Sequence[HunterSpec] = DEFAULT_HUNTERS,
        parallel: bool = True,
        max_concurrency: int = 4,
        log_fn: LogFn | None = None,
    ) -> list[ClipCandidate]:
        working = transcript
        if len(transcript) > 500:
            working = condense_for_prompt(list(transcript), max_segments=500)
            if log_fn:
                log_fn(
                    f"Hunters: condensed transcript {len(transcript)} → {len(working)}"
                )

        async def _run_one(hunter: HunterSpec) -> list[ClipCandidate]:
            if log_fn:
                log_fn(f"Hunter '{hunter.tag.value}' scanning...")
            prompt = prompts.build_hunter_prompt(
                aspect=hunter.tag.value,
                aspect_description=hunter.description,
                transcript=working,
                signals=signals,
                instructions=instructions,
                video_duration=video_duration,
                min_clip=min_clip,
                max_clip=max_clip,
            )
            try:
                text = await self._client.generate(
                    prompt,
                    max_output_tokens=32768,
                    log_fn=log_fn,
                    log_label=f"Hunter[{hunter.tag.value}]",
                )
            except Exception as exc:
                if log_fn:
                    log_fn(f"Hunter '{hunter.tag.value}' failed: {exc}")
                return []

            cands = parse_candidates_json(
                text,
                min_duration=min_clip,
                max_duration=max_clip,
                hunter=hunter.tag,
            )
            if log_fn:
                log_fn(f"Hunter '{hunter.tag.value}' found {len(cands)} candidate(s)")
            return cands

        if parallel:
            # Bounded concurrency — Gemini key rotation handles single-key
            # 429s, but firing 8 hunters at once still risks per-project
            # quota throttling. Cap parallel calls so the practical
            # speed-up (8 sequential ≈ 60 s → bounded ≈ 15-20 s) stays
            # without saturating the rate-limit budget.
            sem = asyncio.Semaphore(max(1, max_concurrency))

            async def _run_bounded(h: HunterSpec) -> list[ClipCandidate]:
                async with sem:
                    return await _run_one(h)

            results = await asyncio.gather(*(_run_bounded(h) for h in hunters))
        else:
            results = []
            for h in hunters:
                results.append(await _run_one(h))

        merged: list[ClipCandidate] = []
        for r in results:
            merged.extend(r)
        merged = deduplicate_candidates(merged, overlap_ratio=0.5)
        if log_fn:
            log_fn(f"Hunters merged: {len(merged)} unique candidates")
        return merged


__all__ = ["HunterSpec", "DEFAULT_HUNTERS", "HunterRunner"]
