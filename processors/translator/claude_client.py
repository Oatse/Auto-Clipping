"""
processors.translator.claude_client — Claude (via OpenAI-compatible router)
calls + prompt building.

Drop-in alternative to :mod:`.gemini_client` used when Gemini is rate-limited
(HTTP 503 storms on gemini-3.5-flash / gemini-2.5-flash). Two top-level
coroutines that match the Gemini client signature exactly so the orchestrator
can swap them transparently:

* :func:`call_claude_translate`  — translate a list of plain texts.
* :func:`call_claude_regroup`    — group word-level data into subtitle
  segments AND translate them. Returns ``(groups, best_partial_groups)``
  where ``groups`` is a list of ``{indices, translated}`` dicts suitable
  for :mod:`processors.translator.regrouper`.

The endpoint is the OpenAI-compatible ``/v1/chat/completions`` route — the
9router (Kiro Pro) proxy default. The exact same anti-AI baseline +
preset blocks from the Gemini client are reused so tone / register / fan-sub
conventions stay identical to the production Gemini path.
"""

from __future__ import annotations

import json
import re

import httpx
from loguru import logger

import config
from models.transcript import WordTimestamp

# Reuse the prompt builders + system instruction from the Gemini client so
# both backends produce subtitles with the same voice and conventions.
# We only swap the transport / response-shape handling.
from .gemini_client import (
    _build_regroup_prompt,
    _build_system_instruction,
    _build_translate_prompt,
    repair_truncated_json,
)


# ── HTTP helpers ──────────────────────────────────────────────────────────


def _chat_completions_url() -> str:
    """Build the OpenAI-compatible chat/completions URL."""
    base = config.NINEROUTER_BASE_URL.rstrip("/")
    return f"{base}/chat/completions"


def _auth_headers() -> dict[str, str]:
    """Authorisation + content headers for 9router."""
    headers = {"Content-Type": "application/json"}
    key = config.NINEROUTER_API_KEY.strip() if config.NINEROUTER_API_KEY else ""
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _strip_code_fence(raw_text: str) -> str:
    """Trim ``` fences and trailing commas Gemini-style.

    Some routed Claude responses wrap JSON in ```json ... ``` even when asked
    not to — strip those defensively before json.loads.
    """
    text = raw_text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json|JSON)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    text = re.sub(r",(\s*[\]}])", r"\1", text)
    return text


def _extract_json_block(raw_text: str) -> str:
    """Pull the first balanced JSON array/object out of free-form text.

    Claude (especially +Thinking variants) sometimes prepends a short
    explanation before the array. Search for the first '[' or '{' and the
    matching closing bracket so json.loads doesn't choke.
    """
    text = _strip_code_fence(raw_text)
    if text.startswith("[") or text.startswith("{"):
        return text

    # Find first array or object opener.
    arr_start = text.find("[")
    obj_start = text.find("{")
    if arr_start == -1 and obj_start == -1:
        return text

    if arr_start == -1:
        start = obj_start
    elif obj_start == -1:
        start = arr_start
    else:
        start = min(arr_start, obj_start)

    return text[start:].strip()


# ── Translate (plain text batch) ──────────────────────────────────────────


async def call_claude_translate(
    texts: list[str],
    target_lang_name: str,
    api_keys: list[str],  # accepted for API parity with Gemini client; unused
    style_preset: str = "natural",
    style_note: str | None = None,
    spicy_filter: bool = False,
) -> list[str] | None:
    """Translate a batch of plain texts via Claude over the 9router proxy.

    Returns the translated list, or ``None`` when the call failed and the
    caller should fall back to DeepL.

    The ``api_keys`` parameter is accepted (and ignored) so the orchestrator
    can swap this function in for ``call_gemini_translate`` without changing
    its call site. The actual API key is read from ``config.NINEROUTER_API_KEY``.
    """
    if not config.NINEROUTER_API_KEY:
        logger.error(
            "Claude backend selected but NINEROUTER_API_KEY is not set — "
            "cannot translate via 9router. Configure .env or switch back "
            "to the Gemini backend.",
        )
        return None

    user_prompt = _build_translate_prompt(texts, target_lang_name)
    system_instruction = _build_system_instruction(
        target_lang_name, style_preset, style_note,
        spicy_filter=spicy_filter,
    )

    payload = {
        "model": config.TRANSLATOR_CLAUDE_MODEL,
        "messages": [
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,
        "max_tokens": 8192,
        # 9router defaults to SSE streaming for chat/completions; force a
        # single JSON body so r.json() works and the salvage / parse paths
        # receive a complete object.
        "stream": False,
    }

    url = _chat_completions_url()
    headers = _auth_headers()

    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            response = await client.post(url, json=payload, headers=headers)
    except (httpx.TimeoutException, httpx.ConnectError) as exc:
        logger.error("Claude translate network error: {}", exc)
        return None

    if response.status_code != 200:
        logger.error(
            "Claude translate API error (HTTP {}): {}",
            response.status_code,
            response.text[:300],
        )
        return None

    try:
        result = response.json()
    except json.JSONDecodeError as exc:
        logger.error("Claude translate non-JSON response: {}", exc)
        return None

    choices = result.get("choices", [])
    if not choices:
        logger.warning("Claude translate returned no choices")
        return None

    raw_text = choices[0].get("message", {}).get("content", "") or ""
    if not raw_text.strip():
        logger.warning("Claude translate returned empty content")
        return None

    cleaned = _extract_json_block(raw_text)
    try:
        translated = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        logger.warning(
            "Claude translate JSON parse error: {} (snippet: {})",
            exc, cleaned[:200],
        )
        return None

    if isinstance(translated, list) and len(translated) == len(texts):
        return [str(t) for t in translated]

    if isinstance(translated, list):
        logger.warning(
            "Claude translation count mismatch: got {} expected {}",
            len(translated), len(texts),
        )
        result_list = [str(t) for t in translated]
        while len(result_list) < len(texts):
            result_list.append(texts[len(result_list)])
        return result_list[: len(texts)]

    logger.warning("Unexpected Claude translate response: {}", raw_text[:200])
    return None


# ── Regroup (word-level) ──────────────────────────────────────────────────


async def call_claude_regroup(
    words: list[WordTimestamp],
    speakers: list[str],
    target_lang_name: str,
    api_keys: list[str],  # accepted for API parity; unused
    style_preset: str = "natural",
    style_note: str | None = None,
    spicy_filter: bool = False,
) -> tuple[list[dict] | None, list[dict] | None]:
    """Word-level grouping + translation via Claude over the 9router proxy.

    Returns ``(groups, best_partial_groups)`` matching the Gemini client
    signature so the orchestrator's salvage path keeps working unchanged.
    """
    if not config.NINEROUTER_API_KEY:
        logger.error(
            "Claude backend selected but NINEROUTER_API_KEY is not set — "
            "cannot regroup via 9router.",
        )
        return None, None

    user_prompt = _build_regroup_prompt(words, speakers, target_lang_name)
    system_instruction = _build_system_instruction(
        target_lang_name, style_preset, style_note,
        spicy_filter=spicy_filter,
    )

    payload = {
        "model": config.TRANSLATOR_CLAUDE_MODEL,
        "messages": [
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,
        # Regroup batches up to ~250 words → JSON output much larger than
        # plain translate. Match Gemini's 32k ceiling so we don't truncate
        # before the salvage path can recover.
        "max_tokens": 32768,
        # 9router defaults to SSE; force a single JSON response body.
        "stream": False,
    }

    url = _chat_completions_url()
    headers = _auth_headers()

    best_partial_groups: list[dict] | None = None

    try:
        async with httpx.AsyncClient(timeout=300.0) as client:
            response = await client.post(url, json=payload, headers=headers)
    except (httpx.TimeoutException, httpx.ConnectError) as exc:
        logger.error("Claude regroup network error: {}", exc)
        return None, best_partial_groups

    if response.status_code != 200:
        logger.error(
            "Claude regroup API error (HTTP {}): {}",
            response.status_code,
            response.text[:300],
        )
        return None, best_partial_groups

    try:
        result = response.json()
    except json.JSONDecodeError as exc:
        logger.error("Claude regroup non-JSON response: {}", exc)
        return None, best_partial_groups

    choices = result.get("choices", [])
    if not choices:
        logger.warning("Claude regroup returned no choices")
        return None, best_partial_groups

    finish_reason = choices[0].get("finish_reason", "") or ""
    raw_text = choices[0].get("message", {}).get("content", "") or ""
    if not raw_text.strip():
        logger.warning("Claude regroup returned empty content")
        return None, best_partial_groups

    cleaned = _extract_json_block(raw_text)
    is_truncated = finish_reason in ("length", "max_tokens")

    if is_truncated:
        logger.warning(
            "Claude regroup output truncated (finish_reason={}). "
            "Attempting to salvage partial JSON...",
            finish_reason,
        )
        salvaged = repair_truncated_json(cleaned)
        if salvaged:
            best_partial_groups = salvaged
        return None, best_partial_groups

    try:
        groups = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        logger.warning(
            "Claude regroup JSON parse error: {} (snippet: {})",
            exc, cleaned[:200],
        )
        salvaged = repair_truncated_json(cleaned)
        if salvaged:
            best_partial_groups = salvaged
        return None, best_partial_groups

    if not isinstance(groups, list) or not groups:
        logger.warning("Unexpected Claude regroup response: {}", raw_text[:200])
        return None, best_partial_groups

    return groups, best_partial_groups
