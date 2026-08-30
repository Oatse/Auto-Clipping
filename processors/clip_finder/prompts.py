"""
processors/clip_finder/prompts.py — Prompt construction for the clip detector.

All prompt-engineering logic lives here so the detector module stays
focused on orchestration. Two builders are exposed:

  - build_detection_prompt   : Phase-1 multi-pass clip discovery
  - build_recheck_prompt     : Phase-1.5 rescue of discarded segments
  - build_hunter_prompt      : Tier-3 single-aspect Hunter (Pola A)
  - build_scoring_prompt     : Tier-3 LLM scoring rubric

Each function returns a fully-formed string ready to send to Gemini.
The shape of the JSON response expected from Gemini is also documented
inside each prompt so the LLM has a stable contract.
"""

from __future__ import annotations

from typing import Sequence

from models.clip import ClipCandidate, SignalEvent

from .heuristics import fmt_time
from .transcript import Segment


# ─── Shared rendering helpers ─────────────────────────────────────────────────


def render_transcript(segments: Sequence[Segment]) -> str:
    """Format segments as `[start.0s - end.0s] text\\n` lines."""
    parts: list[str] = []
    for seg in segments:
        s = round(seg["start"], 1)
        e = round(seg["end"], 1)
        parts.append(f"[{s}s - {e}s] {seg['text']}")
    return "\n".join(parts)


def render_signals(signals: Sequence[SignalEvent], limit: int = 60) -> str:
    """Format signal events as a compact bullet list. Truncated for token budget."""
    if not signals:
        return "  (no multimodal signals available)"

    # Prioritise by intensity, take top-N
    sorted_signals = sorted(signals, key=lambda e: -e.intensity)[:limit]
    sorted_signals.sort(key=lambda e: e.start)

    rows = []
    for e in sorted_signals:
        rng = f"[{round(e.start, 1)}s-{round(e.end, 1)}s]"
        intensity = f"i={e.intensity:.2f}" if e.intensity else ""
        sample = f' "{e.sample[:40]}"' if e.sample else ""
        rows.append(
            f"  {rng} {e.kind.value} {intensity} {e.label}{sample}".strip()
        )
    return "\n".join(rows)


# ─── Detection prompt ─────────────────────────────────────────────────────────


def build_detection_prompt(
    *,
    transcript: Sequence[Segment],
    instructions: str,
    video_duration: float,
    min_clip: float,
    max_clip: float,
    signals: Sequence[SignalEvent] | None = None,
) -> str:
    """Build the main clip-detection prompt.

    VTuber-refocus Step 1: the app is VTuber-only, so the VTuber schema
    (highlight_type + dead_air) and clip-craft rules (buildup / full
    cycle / dead air) are always emitted — no more ``is_vtuber_mode``
    gate that left generic runs without them.
    """
    transcript_text = render_transcript(transcript)
    signals_text = render_signals(signals or [])

    effective_instructions = instructions.strip() if instructions else (
        "Find ALL interesting, notable, funny, exciting, or important moments in "
        "this video. Include highlights, key points, memorable quotes, dramatic "
        "moments, and anything a viewer would want to clip and share."
    )

    schema_extra = (
        '- "highlight_type": category — one of: '
        '"karma_arc" (overconfidence → fail), '
        '"genuine_reaction" (non-scripted scare/laughter/rant), '
        '"clutch_play" (epic play or epic fail), '
        '"chaotic_plea" (screaming/begging/panic), '
        '"collab_dynamic" (banter/chemistry between talents), '
        '"emotional" (touching / heartfelt / vulnerable), '
        '"other"\n'
        '- "dead_air_timestamps": list of seconds where silence longer '
        "than 5 seconds occurs INSIDE this clip's range. Empty list [] if none.\n"
    )
    rules_extra = (
        "- BUILDUP: Each clip MUST start 15-45 seconds before the peak moment "
        "(the 'calm before the storm'). Include narrative hooks.\n"
        "- FULL CYCLE: Each clip MUST include the Aftermath — the speaker's "
        "reaction AFTER the peak event. Never cut mid-climax.\n"
        "- DEAD AIR: Flag silence runs > 5 seconds in dead_air_timestamps.\n"
        "- HIGHLIGHT TYPE: Tag each clip with its highlight_type.\n"
    )

    example = (
        f'[{{"start": 82.0, "end": {82.0 + min_clip}, "title": "Epic moment", '
        '"reason": "Player makes an incredible play", '
        '"highlight_type": "clutch_play", "dead_air_timestamps": []}, '
        f'{{"start": 350.0, "end": {350.0 + min_clip}, "title": "Funny reaction", '
        '"reason": "Hilarious reaction to jumpscare", '
        '"highlight_type": "genuine_reaction", "dead_air_timestamps": [420.5]}]'
    )

    return (
        "You are a video clip finder AI. Given a transcript with timestamps "
        "(in seconds) and instructions, return a JSON array of clip ranges.\n\n"
        "IMPORTANT: Timestamps are in SECONDS. [82.0s - 102.0s] = 1m22s to 1m42s.\n\n"
        f"TOTAL VIDEO DURATION: {round(video_duration, 1)} seconds "
        f"({fmt_time(video_duration)})\n\n"
        f"TRANSCRIPT:\n{transcript_text}\n\n"
        f"MULTIMODAL SIGNALS (audio peaks / chat spikes / silence runs):\n{signals_text}\n\n"
        f"INSTRUCTIONS:\n{effective_instructions}\n\n"
        "Return ONLY a valid JSON array of clips. Each clip must have:\n"
        '- "start": number (seconds, e.g. 82.0)\n'
        '- "end": number (seconds, e.g. 262.0)\n'
        '- "title": short UNIQUE title (string, max 50 chars)\n'
        '- "reason": why this matches the instructions (max 100 chars)\n'
        f"{schema_extra}"
        "\nSTRICT RULES:\n"
        f"- Each clip MUST be between {min_clip} and {max_clip} seconds long\n"
        "- start < end\n"
        "- NO overlapping clips — each clip must cover a DIFFERENT time range\n"
        "- NO duplicate clips — UNIQUE title and content\n"
        "- Include context: start a few seconds before, end a few seconds after\n"
        "- Sort by start time\n"
        "- Find as many DISTINCT matching clips as possible\n"
        "- Treat MULTIMODAL SIGNALS as strong hints — moments where audio peaks "
        "AND chat spikes overlap are almost always clip-worthy.\n"
        f"{rules_extra}"
        "\nExample response:\n"
        f"{example}"
    )


# ─── Recheck prompt ───────────────────────────────────────────────────────────


def build_recheck_prompt(
    *,
    discarded: Sequence[Segment],
    selected: Sequence[ClipCandidate],
    instructions: str,
    video_duration: float,
    min_clip: float,
    max_clip: float,
) -> str:
    """Rescue overlooked moments from discarded transcript regions."""
    discarded_text = render_transcript(discarded)
    selected_summary = "\n".join(
        f"  {i+1}. [{c.start:.1f}s - {c.end:.1f}s] \"{c.title}\" — {c.reason}"
        for i, c in enumerate(selected)
    )

    schema_extra = (
        '- "highlight_type": "karma_arc" | "genuine_reaction" | "clutch_play" | '
        '"chaotic_plea" | "collab_dynamic" | "emotional" | "other"\n'
        '- "dead_air_timestamps": list of silence seconds inside the clip\n'
    )

    return (
        "You are a video clip rescue AI. Re-examine PREVIOUSLY DISCARDED "
        "transcript segments and rescue overlooked moments worth clipping.\n\n"
        f"TOTAL VIDEO DURATION: {round(video_duration, 1)}s ({fmt_time(video_duration)})\n\n"
        f"ALREADY SELECTED CLIPS (do NOT duplicate these):\n{selected_summary}\n\n"
        f"DISCARDED TRANSCRIPT SEGMENTS (your focus):\n{discarded_text}\n\n"
        f"ORIGINAL INSTRUCTIONS:\n{instructions}\n\n"
        "RESCUE CHECKLIST — for each discarded segment, check:\n\n"
        '1. **The "Post-Climax" Gem**: Did something funny / touching / notable '
        "happen RIGHT AFTER a main event ended? Quiet apologies, sudden "
        "donations, breaking character, sigh of relief.\n\n"
        "2. **Subtle Personality Traits**: Quirky habits, catchphrases, inside "
        "jokes that are not loud or dramatic but make fan compilations gold.\n\n"
        "3. **Contextual Relevance**: Was a segment skipped just for being "
        "\"too long\" or \"too slow\"? If the buildup is genuinely entertaining, "
        "rescue it. Don't punish slow burns.\n\n"
        '4. **The "Silent" Reaction**: Look for stunned silences, long pauses '
        "after shocking events. 5 seconds of silence can be a clip's best part.\n\n"
        "Return ONLY a valid JSON array of rescued clips. Each clip must have:\n"
        '- "start" (seconds), "end" (seconds), "title" (max 50), "reason" (max 100)\n'
        f"{schema_extra}"
        "\nRULES:\n"
        f"- Duration MUST be between {min_clip} and {max_clip} seconds\n"
        "- Do NOT overlap with already-selected clips above\n"
        "- Only rescue genuine matches; if nothing qualifies, return []\n"
        "- Sort by start time\n"
    )


# ─── Hunter prompt (Pola A) ──────────────────────────────────────────────────


def build_hunter_prompt(
    *,
    aspect: str,
    aspect_description: str,
    transcript: Sequence[Segment],
    signals: Sequence[SignalEvent] | None,
    instructions: str,
    video_duration: float,
    min_clip: float,
    max_clip: float,
) -> str:
    """Single-aspect hunter — finds clips of one specific kind only."""
    transcript_text = render_transcript(transcript)
    signals_text = render_signals(signals or [])

    return (
        f"You are a SPECIALIST clip hunter. Your ONLY job: find {aspect} moments. "
        "Ignore everything else, however interesting.\n\n"
        f"WHAT COUNTS AS A {aspect.upper()} MOMENT:\n{aspect_description}\n\n"
        f"USER'S OVERALL INTENT (only as context, not a filter):\n{instructions or '(none)'}\n\n"
        f"TOTAL VIDEO DURATION: {round(video_duration, 1)}s ({fmt_time(video_duration)})\n\n"
        f"TRANSCRIPT:\n{transcript_text}\n\n"
        f"MULTIMODAL SIGNALS:\n{signals_text}\n\n"
        "Return ONLY a JSON array of clips. Each clip:\n"
        '- "start": number (seconds)\n'
        '- "end": number (seconds)\n'
        '- "title": short UNIQUE title (max 50 chars)\n'
        '- "reason": why it matches THIS specific aspect (max 100 chars)\n'
        f'- "hunter": "{aspect}"  (always exactly this string)\n'
        f"\nRULES:\n"
        f"- Duration between {min_clip} and {max_clip} seconds\n"
        f"- Only emit moments that genuinely match {aspect} — quality over quantity\n"
        "- If nothing matches, return []\n"
        "- Include enough context: setup + climax + brief aftermath\n"
        "- Sort by start time\n"
    )


# ─── Scoring prompt (Tier-3 stage 2) ─────────────────────────────────────────


# Persona line per Scoring Profile. The rater's frame of reference is
# what makes the difference between "technically a good short" and "a
# clip this audience actually watches", so it is set explicitly rather
# than left to the model's generic priors.
_RATER_PERSONA: dict[str, str] = {
    "vtuber": (
        "You are a senior editor for a VTuber clip channel. Your audience is "
        "existing fans who watch clip compilations daily — they have seen "
        "thousands of stream highlights and are bored by generic ones. They "
        "come for personality, not for events: the line they will quote in "
        "the comments, the moment the performer's real self slips past the "
        "persona, the noise that becomes a community in-joke. A moment can "
        "be loud, dramatic, and completely worthless to them."
    ),
    "gaming": (
        "You are an editor for a gaming highlights channel. Your audience "
        "wants plays and reactions with a clear setup and payoff, and can "
        "tell a genuinely impressive moment from a merely noisy one."
    ),
    "podcast": (
        "You are an editor cutting a long-form podcast into shareable "
        "segments. Your audience wants one self-contained idea per clip."
    ),
    "news": (
        "You are a news producer cutting broadcast segments. Your audience "
        "wants the substance up front and full context inside the clip."
    ),
    "asmr": (
        "You are an editor for an ASMR channel. Your audience wants "
        "sustained, consistent, comfortable sections — not peaks."
    ),
}

_CALIBRATION = """SCORING CALIBRATION — read before scoring anything.
Use the WHOLE 0-10 range. Anchors:
  0-2  Nothing happens. Filler, setup with no payoff, dead air, admin talk.
  3-4  Mildly pleasant but forgettable. A small chuckle, a routine reaction.
       MOST candidates in a typical stream belong here.
  5-6  Solid. Worth including in a long compilation, not worth its own upload.
  7-8  Strong. You would give this its own upload and expect it to do well.
  9-10 Exceptional. The kind of moment that gets re-uploaded by other
       channels and quoted for months. There is usually AT MOST ONE of
       these in an entire stream — often zero.

Grade on a curve across the candidate set: rank them against EACH OTHER,
not against an absolute ideal. If every candidate lands in 6-8 you have
not actually rated anything, and your output is useless. Push weak
candidates down into 2-4 even when they are pleasant.

Loudness is NOT quality. A scream with no context scores low on
quotability and character_moment even if the audio peak is huge. Do not
let a signal summary talk you into a high score on its own."""


def build_scoring_prompt(
    *,
    candidates: Sequence[ClipCandidate],
    transcript: Sequence[Segment],
    instructions: str,
    signals: Sequence[SignalEvent] = (),
    profile: str = "vtuber",
) -> str:
    """Ask the LLM to rate each candidate on 8 axes (0-10).

    Per-candidate signal summaries are injected so the LLM can
    cross-reference what the ear / chat already vouches for. The
    summary is short (1 line per candidate) so token cost stays
    proportional to N candidates — see May-28 audit "#11".

    ``profile`` selects the rater persona. The five original dimensions
    describe a generic short; the three added ones (quotability,
    character_moment, novelty) are what separate a clip a VTuber
    audience re-watches from one that merely contains an event.
    """
    cand_lines = []
    for i, c in enumerate(candidates):
        sig_summary = _signals_summary_for(c, signals)
        suffix = f" — signals: {sig_summary}" if sig_summary else ""
        cand_lines.append(
            f"  {i+1}. [{c.start:.1f}s-{c.end:.1f}s] hunter={c.hunter.value} "
            f"\"{c.title}\" — {c.reason}{suffix}"
        )
    cand_text = "\n".join(cand_lines)
    transcript_text = render_transcript(transcript)

    signals_note = (
        "\nSIGNALS LEGEND: each candidate may carry a summary of audio "
        "peaks (loud bursts), chat spikes (live-chat msgs/sec above "
        "baseline) with QUOTED chat messages, emote storms, superchats, "
        "scene cuts, and CLIP REQUESTS — moments where chat literally "
        "typed 'clip it' / '切り抜き'. Weigh these as follows:\n"
        "  * Chat requesting a clip is the single strongest evidence "
        "available: the audience nominated the moment themselves. "
        "Treat it as near-proof of high replayability and quotability.\n"
        "  * QUOTED chat text tells you WHY chat reacted — a spike of "
        "greetings means nothing, a spike of 'SHE DID NOT JUST SAY THAT' "
        "means everything. Read the quotes, do not just count the spike.\n"
        "  * An audio peak on its own is weak evidence. Loud is not good.\n"
        if signals else ""
    )

    persona = _RATER_PERSONA.get(
        (profile or "vtuber").lower(), _RATER_PERSONA["vtuber"]
    )

    return (
        f"{persona}\n\n"
        "For EACH candidate clip below, score eight qualitative dimensions "
        "on a 0-10 scale and return a JSON array.\n\n"
        f"{_CALIBRATION}\n\n"
        "DIMENSIONS:\n"
        "- retention_hook (0-10): Strength of the FIRST 3 seconds as a hook. "
        "10 = stops scrolling instantly, 0 = boring intro.\n"
        "- emotional_intensity (0-10): Peak emotional payoff (joy/shock/anger/etc). "
        "Genuine, uncontrolled emotion scores high; performed emotion does not.\n"
        "- completeness (0-10): Does it have setup → climax → aftermath? "
        "A clip cut before the reaction lands is incomplete no matter how good "
        "the peak was.\n"
        "- replayability (0-10): Would someone watch this a SECOND time?\n"
        "- shorts_friendly (0-10): Self-contained, no external context needed.\n"
        "- quotability (0-10): Is there a specific line, scream, mispronunciation, "
        "or noise that viewers will repeat in comments, turn into a thumbnail, or "
        "make into an in-joke? A moment can be very funny live and score 2 here "
        "if there is nothing to quote. This is the dimension that separates a "
        "clip that spreads from one that does not.\n"
        "- character_moment (0-10): Does the performer's real personality break "
        "through — losing composure, an unguarded opinion, breaking character, "
        "genuine vulnerability, an unhinged tangent? Routine on-brand behaviour "
        "scores low. This is what fans actually subscribe for.\n"
        "- novelty (0-10): Compared to the OTHER candidates in this list, how "
        "different is this moment? If three candidates are all 'screams at a "
        "jumpscare', the best one keeps its score and the others drop to 2-3. "
        "Repetition is the main reason a compilation feels boring.\n"
        f"{signals_note}"
        f"\nUSER INTENT: {instructions or '(none)'}\n\n"
        f"TRANSCRIPT (for context):\n{transcript_text}\n\n"
        f"CANDIDATES:\n{cand_text}\n\n"
        "Return ONLY a JSON array. Each object has:\n"
        '- "index": 1-based candidate number\n'
        '- "retention_hook": number 0-10\n'
        '- "emotional_intensity": number 0-10\n'
        '- "completeness": number 0-10\n'
        '- "replayability": number 0-10\n'
        '- "shorts_friendly": number 0-10\n'
        '- "quotability": number 0-10\n'
        '- "character_moment": number 0-10\n'
        '- "novelty": number 0-10\n'
        '- "punchline_seconds_from_start": number — seconds from the '
        "candidate start to the payoff beat (the word/phrase the viewer "
        "is here for). Use null when the candidate has no clear single "
        "punchline (e.g. a slow exchange that builds throughout).\n"
        '- "comment": one-sentence rationale (max 120 chars) naming the '
        "specific beat that earned the score — not a restatement of the title.\n"
        "Order by index ascending. Before you emit the array, check your own "
        "spread: if more than half your totals sit within 2 points of each "
        "other, re-score with the anchors above."
    )


def _signals_summary_for(
    candidate: ClipCandidate,
    signals: Sequence[SignalEvent],
) -> str:
    """One-line summary of signals overlapping the candidate's range.

    Uses ``SignalKind`` lazily so the prompts module stays import-light
    when the model layer isn't loaded.
    """
    if not signals:
        return ""
    from models.clip import SignalKind

    counts: dict[str, int] = {}
    max_peak_db: float = 0.0
    max_chat_ratio: float = 0.0
    # What chat said, not just how much of it there was. Capped at a few
    # short excerpts so the per-candidate line stays one line even when a
    # long clip overlaps many spikes.
    chat_quotes: list[str] = []
    clip_requests: list[str] = []
    for s in signals:
        if s.end < candidate.start or s.start > candidate.end:
            continue
        kind = s.kind.value
        counts[kind] = counts.get(kind, 0) + 1
        if s.kind == SignalKind.CHAT_CLIP_INTENT:
            if s.sample:
                clip_requests.append(s.sample)
            continue
        if s.kind == SignalKind.CHAT_SPIKE and s.sample:
            chat_quotes.append(s.sample)
        if s.kind == SignalKind.AUDIO_PEAK:
            try:
                # label e.g. "+18.5 dB above baseline"
                num = (s.label or "").split()[0].lstrip("+")
                max_peak_db = max(max_peak_db, float(num))
            except (ValueError, IndexError):
                pass
        elif s.kind == SignalKind.CHAT_SPIKE:
            try:
                if "x baseline" in (s.label or ""):
                    num = (s.label or "").replace("chat", "").split("x")[0].strip()
                    max_chat_ratio = max(max_chat_ratio, float(num))
            except (ValueError, IndexError):
                pass

    if not counts:
        return ""
    parts: list[str] = []
    if SignalKind.AUDIO_PEAK.value in counts:
        peak = counts[SignalKind.AUDIO_PEAK.value]
        parts.append(
            f"{peak} audio peak(s) (max +{max_peak_db:.1f} dB)"
            if max_peak_db else f"{peak} audio peak(s)"
        )
    if SignalKind.CHAT_SPIKE.value in counts:
        spike = counts[SignalKind.CHAT_SPIKE.value]
        parts.append(
            f"chat {max_chat_ratio:.1f}x baseline"
            if max_chat_ratio else f"{spike} chat spike(s)"
        )
    if SignalKind.CHAT_EMOTE_STORM.value in counts:
        parts.append(f"{counts[SignalKind.CHAT_EMOTE_STORM.value]} emote storm(s)")
    if SignalKind.CHAT_SUPERCHAT.value in counts:
        parts.append(f"{counts[SignalKind.CHAT_SUPERCHAT.value]} superchat(s)")
    if SignalKind.SCENE_CUT.value in counts:
        parts.append(f"{counts[SignalKind.SCENE_CUT.value]} scene cut(s)")
    if SignalKind.CHAT_CLIP_INTENT.value in counts:
        n = counts[SignalKind.CHAT_CLIP_INTENT.value]
        parts.append(f"** CHAT REQUESTED A CLIP HERE ({n} burst(s)) **")
    if clip_requests:
        parts.append(f'clip requests: "{" / ".join(clip_requests[:2])[:120]}"')
    if chat_quotes:
        parts.append(f'chat said: "{" / ".join(chat_quotes[:2])[:160]}"')
    return ", ".join(parts)


__all__ = [
    "render_transcript",
    "render_signals",
    "build_detection_prompt",
    "build_recheck_prompt",
    "build_hunter_prompt",
    "build_scoring_prompt",
]
