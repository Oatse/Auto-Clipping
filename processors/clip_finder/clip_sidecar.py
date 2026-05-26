"""
processors/clip_finder/clip_sidecar.py — Generate upload-ready Clip metadata.

After a Clip is rendered the user usually has to invent a title,
description, hashtags, and pick a thumbnail timestamp before uploading
to TikTok / YT Shorts / Reels. We can do that for them: send the Clip's
own transcript window to Gemini once and write the result as a
sidecar JSON next to the MP4.

CONTEXT.md: see "Clip Sidecar" definition.

Public API:
    generate(*, clip, transcript_window, api_keys, ...) -> ClipSidecar
    write(sidecar, clip_path)                            -> Path

The sidecar shape is intentionally narrow — only fields a creator pastes
into upload forms. We do NOT bake in platform-specific quirks (e.g. YT
description vs TikTok caption length); the renderer / UI layer trims
to platform limits at display time.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Sequence

LogFn = Callable[[str], None]


# ─── Sidecar shape ───────────────────────────────────────────────────────────


@dataclass
class ClipSidecar:
    """Upload-ready metadata for a single Clip.

    Fields:
        title          — punchy, ≤ 70 chars (TikTok/YT Shorts caption length).
        description    — 1-2 sentence pitch, ≤ 280 chars.
        hashtags       — list of strings without the leading '#'. Caller
                         decides whether to render them with '#'.
        suggested_thumbnail_t — seconds offset *into the Clip* (not the
                         source video) where the most expressive frame
                         lives. UI uses this to grab a JPEG via FFmpeg.
        emoji_hint     — single emoji that summarises the Clip's vibe.
                         Optional. Renderer can prepend to the title.
        language       — BCP-47 code matching the language Gemini wrote
                         the metadata in. Defaults to 'en'.
    """

    title: str
    description: str
    hashtags: list[str] = field(default_factory=list)
    suggested_thumbnail_t: float = 0.5
    emoji_hint: str = ""
    language: str = "en"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["title"] = self.title[:70]
        d["description"] = self.description[:280]
        # Strip stray hashes the model might have produced.
        d["hashtags"] = [h.lstrip("#").strip() for h in self.hashtags if h.strip()]
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "ClipSidecar":
        return cls(
            title=str(data.get("title", ""))[:70],
            description=str(data.get("description", ""))[:280],
            hashtags=[
                str(h).lstrip("#").strip()
                for h in (data.get("hashtags") or [])
                if str(h).strip()
            ],
            suggested_thumbnail_t=float(data.get("suggested_thumbnail_t", 0.5)),
            emoji_hint=str(data.get("emoji_hint", ""))[:8],
            language=str(data.get("language", "en")),
        )


# ─── Prompt + parser ─────────────────────────────────────────────────────────


_PROMPT_TEMPLATE = """\
You are a short-form video editor writing upload-ready metadata for a clip.

CLIP TITLE (working): {working_title}
CLIP REASON (working): {working_reason}
CLIP DURATION: {duration:.1f}s
CLIP TRANSCRIPT:
{transcript_text}

Return strict JSON with exactly these keys:
{{
  "title": "≤ 60 chars, hook-first, no clickbait, must reflect what's in the clip",
  "description": "≤ 240 chars, 1-2 sentences, plain text",
  "hashtags": ["3-6 lowercase tags, no '#' prefix, no spaces"],
  "suggested_thumbnail_t": 0.0,  // seconds INTO the clip, must be in [0, duration]
  "emoji_hint": "one emoji that captures the vibe, or empty string",
  "language": "BCP-47 code matching the language you wrote the title/description in"
}}

Rules:
- Output JSON only, no markdown fences, no surrounding prose.
- Do NOT invent details that are not in the transcript.
- Hashtags must be relevant to the content; do not list trending tags
  unrelated to the clip just to game algorithms.
- suggested_thumbnail_t should land on the most expressive moment —
  typically the punchline, the reaction face, or the climax beat.
"""


def _build_prompt(
    *,
    working_title: str,
    working_reason: str,
    duration: float,
    transcript_text: str,
) -> str:
    return _PROMPT_TEMPLATE.format(
        working_title=working_title or "(no title)",
        working_reason=working_reason or "(no reason)",
        duration=max(0.0, duration),
        transcript_text=transcript_text.strip()[:4000],
    )


_JSON_BLOCK_RE = re.compile(r"\{[^{}]*\}", re.DOTALL)


def _parse_response(raw: str) -> dict:
    """Best-effort JSON extraction from Gemini response.

    Falls back to ``{}`` when nothing parses — the caller turns that
    into a default sidecar so a parse failure never breaks render.
    """
    if not raw:
        return {}
    text = raw.strip()
    # Strip code fences if present.
    if text.startswith("```"):
        # remove ```json\n ... ``` wrapper
        text = re.sub(r"^```[a-zA-Z]*\n", "", text).rstrip("`").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Salvage: take the first JSON-looking block.
    match = _JSON_BLOCK_RE.search(text)
    if not match:
        return {}
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}


# ─── Public API ──────────────────────────────────────────────────────────────


async def generate(
    *,
    clip_title: str,
    clip_reason: str,
    clip_duration: float,
    transcript_window: Sequence[dict] | str,
    api_keys: list[str],
    gemini_model: str = "gemini-3.5-flash",
    fallback_models: Sequence[str] = (),
    log_fn: LogFn | None = None,
) -> ClipSidecar:
    """Generate an upload-ready ``ClipSidecar`` via Gemini.

    On any failure — bad API key, parse error, empty response — returns
    a default sidecar derived from ``clip_title`` / ``clip_reason`` so
    callers never have to handle exceptions.
    """
    transcript_text = _flatten_transcript(transcript_window)
    if not transcript_text:
        if log_fn:
            log_fn("Sidecar: empty transcript window, using working title fallback")
        return _default_sidecar(clip_title, clip_reason, clip_duration)

    if not api_keys:
        if log_fn:
            log_fn("Sidecar: no Gemini API keys, using working title fallback")
        return _default_sidecar(clip_title, clip_reason, clip_duration)

    # Lazy import — keeps this module importable in environments where
    # the gemini client deps aren't present.
    from .gemini_client import GeminiClient

    client = GeminiClient(
        list(api_keys),
        model=gemini_model,
        fallback_models=list(fallback_models),
    )

    prompt = _build_prompt(
        working_title=clip_title,
        working_reason=clip_reason,
        duration=clip_duration,
        transcript_text=transcript_text,
    )
    try:
        raw = await client.generate(
            prompt,
            max_output_tokens=2048,
            log_fn=log_fn,
            log_label="Sidecar",
        )
    except Exception as exc:  # noqa: BLE001 — never crash render.
        if log_fn:
            log_fn(f"Sidecar: Gemini call failed: {exc}")
        return _default_sidecar(clip_title, clip_reason, clip_duration)

    parsed = _parse_response(raw)
    if not parsed:
        if log_fn:
            log_fn("Sidecar: empty / unparsable response, using fallback")
        return _default_sidecar(clip_title, clip_reason, clip_duration)

    sidecar = ClipSidecar.from_dict(parsed)
    sidecar.suggested_thumbnail_t = max(
        0.0, min(clip_duration, sidecar.suggested_thumbnail_t)
    )
    return sidecar


def write(sidecar: ClipSidecar, clip_path: Path) -> Path:
    """Write ``sidecar`` next to ``clip_path`` as ``{stem}.metadata.json``.

    Returns the path written. Existing files are overwritten — sidecar
    is always derived from the most recent metadata generation, never
    user-edited (the UI surfaces a copy-button, not an editor).
    """
    sidecar_path = clip_path.with_suffix(".metadata.json")
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar_path.write_text(
        json.dumps(sidecar.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return sidecar_path


def read(clip_path: Path) -> ClipSidecar | None:
    """Read the sidecar associated with ``clip_path``, or None if missing."""
    sidecar_path = clip_path.with_suffix(".metadata.json")
    if not sidecar_path.exists():
        return None
    try:
        data = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return ClipSidecar.from_dict(data)


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _flatten_transcript(window: Sequence[dict] | str) -> str:
    """Coerce a transcript window into a single newline-joined text block."""
    if isinstance(window, str):
        return window.strip()
    if not window:
        return ""
    parts: list[str] = []
    for seg in window:
        if isinstance(seg, dict):
            text = str(seg.get("text", "")).strip()
            if text:
                parts.append(text)
    return "\n".join(parts).strip()


def _default_sidecar(title: str, reason: str, duration: float) -> ClipSidecar:
    """Fallback when Gemini is unavailable / produces nothing useful."""
    safe_title = (title or "Clip").strip()[:60]
    safe_desc = (reason or safe_title).strip()[:240]
    return ClipSidecar(
        title=safe_title,
        description=safe_desc,
        hashtags=[],
        suggested_thumbnail_t=max(0.0, min(duration, duration * 0.4)),
        emoji_hint="",
        language="en",
    )


__all__ = ["ClipSidecar", "generate", "write", "read"]
