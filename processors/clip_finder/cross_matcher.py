"""
processors/clip_finder/cross_matcher.py — LLM cross-source moment matching.

Receives the per-source Clip lists produced by the standard Clip Finder
pipeline and asks the LLM to group them into POVGroups — real-world events
that appear in ≥1 sources.

Public API:

    match_clips_across_sources(
        source_clips,          # list of (source_meta, list[Clip])
        *,
        instructions,
        llm_client,
        log_fn,
    ) -> tuple[list[POVGroup], list[tuple[int, Clip]]]

Returns (pov_groups, unmatched_clips) where unmatched_clips is a list of
(source_idx, Clip) pairs that were not assigned to any group.

Design notes:
  - The matching prompt asks for *indexes only* — it never re-reads the
    full transcript, keeping context window usage proportional to the
    number of Clips × sources (not the transcript length).
  - Validation (invariant B2): each source_idx may appear at most once per
    group. Duplicates are resolved by keeping the higher-scoring clip and
    demoting the other to unmatched.
  - Confidence threshold (Grill #4): groups below CONFIDENCE_THRESHOLD are
    flagged is_multi_pov=False and rendered in the "Single-Source Moments"
    section.
  - JSON salvage: if the LLM returns malformed JSON, a regex extraction
    attempt is made before giving up, matching the pattern used in
    clip_selection.py.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Sequence

from models.clip import Clip, HunterTag
from models.pov_group import (
    CONFIDENCE_THRESHOLD,
    POVGroup,
    POVPerspective,
)

LogFn = Callable[[str], None]


# ─── Source metadata type ─────────────────────────────────────────────────────

# A lightweight dict describing one source — passed alongside its Clip list.
SourceMeta = dict[str, Any]  # {source_idx, url, label, video_title}


# ─── Transcript snippet builder ───────────────────────────────────────────────

_MAX_SNIPPET_SEGMENTS = 6   # how many subtitle lines to include per clip


def _build_transcript_snippet(
    clip: Clip,
    full_transcript: list[dict],
    *,
    max_segments: int = _MAX_SNIPPET_SEGMENTS,
) -> str:
    """Return a short transcript excerpt around the clip's time range."""
    if not full_transcript:
        return ""
    segments = [
        seg for seg in full_transcript
        if seg.get("start", 0) >= clip.start - 2
        and seg.get("end", 0) <= clip.end + 2
    ][:max_segments]
    if not segments:
        return ""
    parts = [
        f"[{round(seg['start'], 1)}s] {seg.get('text', '').strip()}"
        for seg in segments
    ]
    return " … ".join(parts)


# ─── Prompt builder ───────────────────────────────────────────────────────────


def build_cross_matching_prompt(
    source_metas: list[SourceMeta],
    source_clips: list[list[Clip]],
    transcripts: list[list[dict]],
    instructions: str,
) -> str:
    """Build the LLM prompt for cross-source moment matching.

    The prompt feeds a compact JSON summary of all clips from all sources
    and asks the LLM to output a JSON object grouping them by shared event.

    Parameters
    ----------
    source_metas:
        One dict per source with keys {source_idx, url, label, video_title}.
    source_clips:
        Parallel list — source_clips[i] are the Clips from source_metas[i].
    transcripts:
        Parallel list — transcripts[i] is the full transcript of source i,
        used to build transcript snippets per clip.
    instructions:
        The user's original instructions (passed through for context).
    """
    sources_json: list[dict] = []
    for meta, clips, transcript in zip(source_metas, source_clips, transcripts):
        clip_entries: list[dict] = []
        for clip_idx, clip in enumerate(clips):
            snippet = _build_transcript_snippet(clip, transcript)
            clip_entries.append({
                "clip_idx": clip_idx,
                "start": round(clip.start, 1),
                "end": round(clip.end, 1),
                "duration_s": round(clip.duration, 1),
                "title": clip.title,
                "reason": clip.reason[:150] if clip.reason else "",
                "hunter": clip.hunter.value,
                "score_total": round(clip.score.total, 2),
                "transcript_snippet": snippet,
            })
        sources_json.append({
            "source_idx": meta["source_idx"],
            "label": meta.get("label") or meta.get("video_title") or f"Source {meta['source_idx']}",
            "video_title": meta.get("video_title", ""),
            "clips": clip_entries,
        })

    sources_block = json.dumps(sources_json, ensure_ascii=False, indent=2)

    return f"""You are a video editor's assistant. You have been given clips from {len(source_metas)} different YouTube videos that were recorded at the same event (e.g., a multiplayer game session, a podcast with multiple cameras, a live event).

User's instructions: {instructions or "Find all interesting shared moments."}

Your task: identify which clips from different sources capture the SAME real-world event or moment, and group them together.

Here are all the clips:

{sources_block}

Rules:
1. A group represents ONE real-world event seen from different perspectives.
2. Each source_idx may appear AT MOST ONCE per group (one perspective per source per event).
3. A group may have only one clip if no match was found from other sources.
4. Assign a confidence score 0.0–1.0 for how certain you are the clips share the same event.
   - 0.9+: very strong match (same dialogue, same outcome, same explicit references)
   - 0.7–0.9: strong match (same general topic, overlapping timeline evidence)
   - 0.5–0.7: possible match (similar theme but timing/context is unclear)
   - <0.5: weak/speculative — prefer to leave unmatched
5. All clips must appear in exactly one group OR in the "unmatched" array.

Return ONLY a JSON object with NO prose, NO code fences. Schema:

{{
  "pov_groups": [
    {{
      "group_title": "Short descriptive event title (max 60 chars)",
      "group_reason": "Why these clips show the same event (1–2 sentences)",
      "confidence": 0.85,
      "perspectives": [
        {{"source_idx": 0, "clip_idx": 2}},
        {{"source_idx": 1, "clip_idx": 0}}
      ]
    }}
  ],
  "unmatched": [
    {{"source_idx": 0, "clip_idx": 5}}
  ]
}}
"""


# ─── Response parser ─────────────────────────────────────────────────────────


def _try_extract_json(raw_text: str) -> dict | None:
    """Attempt to extract a JSON object from raw LLM output."""
    text = raw_text.strip()

    # Strip markdown code fences if present
    if text.startswith("```"):
        text = re.sub(r"^```(?:json|JSON)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text.strip())
    # Remove trailing commas before ] or }
    text = re.sub(r",(\s*[}\]])", r"\1", text)

    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Regex salvage: find the outermost { ... }
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    return None


def _validate_groups(
    raw_groups: list[dict],
    source_clips: list[list[Clip]],
) -> tuple[list[dict], list[tuple[int, int]]]:
    """Enforce invariant B2: 1 source_idx per group per event.

    For each group, if the same source_idx appears more than once, keep
    the clip with the higher score_total and demote the others to unmatched.

    Returns (validated_groups, extra_unmatched) where extra_unmatched is a
    list of (source_idx, clip_idx) pairs rejected by the validation.
    """
    extra_unmatched: list[tuple[int, int]] = []
    validated: list[dict] = []

    for group in raw_groups:
        perspectives = group.get("perspectives", [])
        seen: dict[int, int] = {}   # source_idx → clip_idx of winner

        for p in perspectives:
            src = int(p.get("source_idx", -1))
            cidx = int(p.get("clip_idx", -1))

            # Bounds check
            if src < 0 or src >= len(source_clips):
                continue
            if cidx < 0 or cidx >= len(source_clips[src]):
                continue

            if src in seen:
                # Collision — keep the higher-scoring one
                existing_cidx = seen[src]
                existing_score = source_clips[src][existing_cidx].score.total
                new_score = source_clips[src][cidx].score.total
                if new_score > existing_score:
                    extra_unmatched.append((src, existing_cidx))
                    seen[src] = cidx
                else:
                    extra_unmatched.append((src, cidx))
            else:
                seen[src] = cidx

        group["perspectives"] = [
            {"source_idx": src, "clip_idx": cidx}
            for src, cidx in seen.items()
        ]
        if group["perspectives"]:
            validated.append(group)

    return validated, extra_unmatched


# ─── Main public function ─────────────────────────────────────────────────────


async def match_clips_across_sources(
    source_metas: list[SourceMeta],
    source_clips: list[list[Clip]],
    transcripts: list[list[dict]],
    *,
    instructions: str,
    llm_client: Any,           # GeminiClient | NineRouterClient — duck-typed
    log_fn: LogFn | None = None,
) -> tuple[list[POVGroup], list[tuple[int, Clip]]]:
    """Match clips from multiple sources into POVGroups via LLM.

    Parameters
    ----------
    source_metas:
        One SourceMeta dict per source.
    source_clips:
        Parallel — source_clips[i] = Clips from source_metas[i].
    transcripts:
        Parallel — transcripts[i] = full transcript of source i.
    instructions:
        User's original search instructions for the LLM context.
    llm_client:
        Pre-built GeminiClient or NineRouterClient instance.
    log_fn:
        Optional logging callback.

    Returns
    -------
    (pov_groups, unmatched)
        pov_groups: list of POVGroup objects (multi AND single-source)
        unmatched: (source_idx, Clip) pairs not in any group
    """
    def log(msg: str) -> None:
        if log_fn:
            log_fn(msg)

    total_clips = sum(len(clips) for clips in source_clips)
    log(f"Cross-matching {total_clips} clips across {len(source_metas)} sources...")

    prompt = build_cross_matching_prompt(
        source_metas, source_clips, transcripts, instructions
    )

    raw_text = await llm_client.generate(
        prompt,
        temperature=0.2,
        max_output_tokens=4096,
        log_fn=log_fn,
        log_label="cross-matching",
    )

    parsed = _try_extract_json(raw_text)
    if not parsed:
        log("Cross-matching: LLM returned unparseable JSON — all clips are unmatched")
        unmatched = [
            (meta["source_idx"], clip)
            for meta, clips in zip(source_metas, source_clips)
            for clip in clips
        ]
        return [], unmatched

    raw_groups: list[dict] = parsed.get("pov_groups", [])
    raw_unmatched: list[dict] = parsed.get("unmatched", [])

    # Validate: enforce 1 source_idx per group
    raw_groups, extra_unmatched_pairs = _validate_groups(raw_groups, source_clips)

    # Track which (source_idx, clip_idx) pairs are assigned to groups
    assigned: set[tuple[int, int]] = set()
    for g in raw_groups:
        for p in g.get("perspectives", []):
            assigned.add((int(p["source_idx"]), int(p["clip_idx"])))

    # Build POVGroup objects
    pov_groups: list[POVGroup] = []
    for g in raw_groups:
        perspectives: list[POVPerspective] = []
        for p in g.get("perspectives", []):
            src_idx = int(p["source_idx"])
            clip_idx = int(p["clip_idx"])
            meta = source_metas[src_idx]
            clip = source_clips[src_idx][clip_idx]
            label = (
                meta.get("label")
                or meta.get("video_title")
                or f"Source {src_idx}"
            )
            perspectives.append(POVPerspective(
                source_idx=src_idx,
                url=meta["url"],
                label=label,
                video_title=meta.get("video_title"),
                start=clip.start,
                end=clip.end,
                title=clip.title,
                reason=clip.reason,
                score=clip.score,
                hunter=clip.hunter,
            ))

        if not perspectives:
            continue

        pov_groups.append(POVGroup.create(
            title=str(g.get("group_title", "Untitled moment")),
            reason=str(g.get("group_reason", "")),
            confidence=float(g.get("confidence", 0.0)),
            perspectives=perspectives,
        ))

    # Collect all unmatched clips
    # From LLM's explicit unmatched list
    raw_unmatched_pairs: set[tuple[int, int]] = set()
    for u in raw_unmatched:
        src = int(u.get("source_idx", -1))
        cidx = int(u.get("clip_idx", -1))
        if 0 <= src < len(source_clips) and 0 <= cidx < len(source_clips[src]):
            raw_unmatched_pairs.add((src, cidx))

    # Plus any demoted by validation
    for src, cidx in extra_unmatched_pairs:
        raw_unmatched_pairs.add((src, cidx))

    # Plus any clips the LLM forgot to mention at all
    all_clip_indices: set[tuple[int, int]] = {
        (src_idx, clip_idx)
        for src_idx, clips in enumerate(source_clips)
        for clip_idx in range(len(clips))
    }
    missing = all_clip_indices - assigned - raw_unmatched_pairs
    all_unmatched_pairs = raw_unmatched_pairs | missing

    unmatched: list[tuple[int, Clip]] = [
        (src, source_clips[src][cidx])
        for src, cidx in sorted(all_unmatched_pairs)
        if 0 <= src < len(source_clips) and 0 <= cidx < len(source_clips[src])
    ]

    multi_pov_count = sum(1 for g in pov_groups if g.is_multi_pov)
    log(
        f"Cross-matching complete: {multi_pov_count} multi-POV group(s), "
        f"{len(pov_groups) - multi_pov_count} single-source group(s), "
        f"{len(unmatched)} unmatched clip(s)"
    )

    return pov_groups, unmatched


__all__ = [
    "build_cross_matching_prompt",
    "match_clips_across_sources",
    "SourceMeta",
]
