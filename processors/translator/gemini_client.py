"""
processors.translator.gemini_client — Gemini API calls + prompt building.

Two top-level coroutines:

* :func:`call_gemini_translate`  — translate a list of plain texts.
* :func:`call_gemini_regroup`    — group word-level data into subtitle
  segments AND translate them.  Returns a list of ``{indices, translated}``
  dicts suitable for :mod:`processors.translator.regrouper`.

Both functions implement key rotation on rate limits, JSON salvage on
truncated output, and structured logging.  They never raise — instead
they return ``None`` (or fall through to a caller-provided fallback).
"""

from __future__ import annotations

import json
import re

import httpx
from loguru import logger

from models.transcript import WordTimestamp

from .constants import GEMINI_API_URL


def _build_translate_prompt(texts: list[str], target_lang_name: str) -> str:
    """Build the Gemini prompt for plain-text batch translation."""
    numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(texts))
    return (
        f"Translate the following numbered lines into {target_lang_name}. "
        "Return ONLY a JSON array of translated strings in the same order. "
        "Preserve the meaning and natural speech style. "
        "If a word or phrase contains stretched/elongated characters that represent "
        "emotional emphasis (e.g. 'noooooo', 'BAKAAAAAAA', 'stoppppp'), "
        "translate it AND preserve that elongation style using the equivalent "
        "stretched form in the target language. "
        "IMPORTANT — do NOT translate lines that consist entirely of expressive "
        "vocalizations, exclamations, onomatopoeia, or romanized non-source-language "
        "sounds that carry no direct lexical meaning. Keep those lines EXACTLY as-is. "
        "Examples of lines to keep unchanged: purely expressive Japanese/Asian romaji "
        "sounds ('soryaaaa', 'kyaaaa', 'uwaaaa', 'yataaa', 'ikuzoooo', 'iyaaaaa'), "
        "universal vocal sounds ('ahhhh', 'ohhhh', 'ehhhh', 'ahahaha', 'ufufufu'), "
        "and sound effects ('zuuun', 'dodododo', 'baaaaam'). "
        "Do NOT add numbering in the output, just the translated text in a JSON array.\n\n"
        f"{numbered}"
    )


def _build_regroup_prompt(
    words: list[WordTimestamp],
    speakers: list[str],
    target_lang_name: str,
) -> str:
    """Build the Gemini prompt for word-level grouping + translation."""
    lines: list[str] = []
    multi_speaker = len(set(speakers)) > 1

    for i, (w, sp) in enumerate(zip(words, speakers)):
        # Pause indicator
        if i > 0:
            gap = w.start - words[i - 1].end
            if gap > 0.7:
                lines.append(f"[PAUSE {gap:.1f}s]")

        parts = [f"{i}. {w.word}"]
        if multi_speaker:
            if i == 0 or speakers[i] != speakers[i - 1]:
                parts.append(f"[{sp}]")
        lines.append(" ".join(parts))

    word_list_text = "\n".join(lines)

    prompt = (
        "You are a subtitle segmentation and translation assistant.\n\n"
        "Below is a numbered list of transcribed words from a video.\n"
        "[PAUSE Xs] markers indicate silence gaps between words.\n"
    )
    if multi_speaker:
        prompt += "[SPEAKER_XX] tags indicate speaker changes.\n"
    prompt += (
        "\nYour tasks:\n"
        "1. GROUP these words into natural subtitle segments:\n"
        "   - Maximum 12 words per subtitle\n"
        "   - Break at sentence boundaries (., !, ?) when possible\n"
        "   - ALWAYS start a new subtitle at [PAUSE] markers\n"
    )
    if multi_speaker:
        prompt += "   - ALWAYS start a new subtitle on speaker changes\n"
    prompt += (
        "   - Keep each subtitle as a complete phrase or sentence\n"
        "   - NEVER create a group whose translation is only punctuation "
        "(e.g. '.', '!', '?'). Always include at least one meaningful word\n"
        f"2. TRANSLATE each subtitle group into {target_lang_name}:\n"
        "   - Preserve emotional elongation "
        "(e.g. 'noooooo' → equivalent stretched form in target language)\n"
        "   - Keep expressive vocalizations EXACTLY as-is: onomatopoeia, "
        "exclamations, romaji sounds ('kyaaaa', 'uwaaaa'), "
        "universal vocal sounds ('ahhhh', 'ohhhh', 'ahahaha')\n"
        "   - The translated text must be a proper subtitle line, never just "
        "punctuation or a single symbol\n"
        "3. FIX any broken/fragmented words:\n"
        "   - If consecutive words look like fragments of one word "
        "(e.g. 'beau' + 'tiful' = 'beautiful', "
        "'un' + 'fortunately' = 'unfortunately'), "
        "treat them as a single word in your translation.\n\n"
        f"Words:\n{word_list_text}\n\n"
        "Return ONLY a strictly valid JSON array where each element is:\n"
        '{"indices": [0, 1, 2], "translated": "translated subtitle text"}\n\n'
        "CRITICAL RULES:\n"
        "- Keep your response concise — use short, natural subtitle translations\n"
        "- ALWAYS escape double quotes inside the translation text using a "
        "backslash (e.g. \\\"Hello\\\")\n"
        "- Do NOT leave trailing commas in JSON arrays or objects\n"
        f"- You MUST include EVERY word index from 0 to {len(words) - 1} — "
        "do NOT skip any index\n"
        "- Every word index must appear in exactly one group\n"
        "- Groups must be in chronological order\n"
        "- Indices within each group must be consecutive\n"
        f"- The total number of indices across all groups must equal {len(words)}\n"
        f"- If the first word is index 0 and the last is index {len(words) - 1}, "
        f"then indices 0, 1, 2, ..., {len(words) - 1} must all be present\n"
    )
    return prompt


def repair_truncated_json(raw_text: str) -> list[dict] | None:
    """Salvage complete ``{"indices": [...], "translated": "..."}`` objects
    from a truncated Gemini response.

    Returns a list of group dicts, or ``None`` if nothing could be salvaged.
    """
    pattern = re.compile(
        r'\{\s*"indices"\s*:\s*\[([\d\s,]+)\]\s*,\s*"translated"\s*:\s*"((?:[^"\\]|\\.)*)"\s*\}',
        re.DOTALL,
    )
    matches = list(pattern.finditer(raw_text))
    if not matches:
        return None

    groups: list[dict] = []
    for m in matches:
        try:
            indices_str = m.group(1).strip()
            indices = [int(x.strip()) for x in indices_str.split(",") if x.strip()]
            translated = m.group(2)
            translated = (
                translated.replace('\\"', '"')
                .replace('\\n', '\n')
                .replace('\\\\', '\\')
            )
            if indices and translated.strip():
                groups.append({"indices": indices, "translated": translated})
        except (ValueError, IndexError):
            continue

    if groups:
        logger.info(
            "Salvaged {} complete group(s) from truncated Gemini JSON",
            len(groups),
        )
        return groups
    return None


async def call_gemini_translate(
    texts: list[str],
    target_lang_name: str,
    api_keys: list[str],
) -> list[str] | None:
    """Translate a batch of plain texts via Gemini.

    Rotates keys on rate-limit/error.  Returns the translated list or
    ``None`` when every key failed (caller should invoke the DeepL fallback).
    """
    prompt = _build_translate_prompt(texts, target_lang_name)
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 8192,
            "responseMimeType": "application/json",
        },
    }

    last_error = None
    for key_idx, api_key in enumerate(api_keys):
        try:
            url = f"{GEMINI_API_URL}?key={api_key}"
            async with httpx.AsyncClient(timeout=120.0) as client:
                response = await client.post(url, json=payload)

            if response.status_code in (429, 403):
                logger.warning(
                    "Gemini Key #{} rate-limited (HTTP {}), trying next...",
                    key_idx + 1,
                    response.status_code,
                )
                last_error = f"HTTP {response.status_code}"
                continue

            if response.status_code != 200:
                logger.error(
                    "Gemini API error (HTTP {}): {}",
                    response.status_code,
                    response.text[:300],
                )
                last_error = f"HTTP {response.status_code}"
                continue

            result = response.json()
            candidates = result.get("candidates", [])
            if not candidates:
                logger.warning("Gemini returned no candidates")
                last_error = "No candidates"
                continue

            content = candidates[0].get("content", {})
            parts = content.get("parts", [])
            raw_text = parts[0].get("text", "") if parts else ""

            translated = json.loads(raw_text)
            if isinstance(translated, list) and len(translated) == len(texts):
                return [str(t) for t in translated]

            if isinstance(translated, list):
                logger.warning(
                    "Translation count mismatch: got {} expected {}",
                    len(translated),
                    len(texts),
                )
                result_list = [str(t) for t in translated]
                while len(result_list) < len(texts):
                    result_list.append(texts[len(result_list)])
                return result_list[: len(texts)]

            logger.warning("Unexpected Gemini response format: {}", raw_text[:200])
            last_error = "Unexpected response format"
            continue

        except json.JSONDecodeError as exc:
            logger.warning("Failed to parse Gemini JSON response: {}", exc)
            last_error = f"JSON parse error: {exc}"
            continue
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            logger.warning("Gemini Key #{} network error: {}", key_idx + 1, exc)
            last_error = str(exc)
            continue

    logger.error(
        "All Gemini API keys failed for translation. Last error: {}", last_error,
    )
    return None


async def call_gemini_regroup(
    words: list[WordTimestamp],
    speakers: list[str],
    target_lang_name: str,
    api_keys: list[str],
) -> tuple[list[dict] | None, list[dict] | None]:
    """Call Gemini to group words into subtitle segments and translate.

    Returns a 2-tuple ``(groups, best_partial_groups)``:

    * ``groups`` — fully-parsed group list when at least one key succeeded
      with a complete JSON response.  ``None`` when every key failed.
    * ``best_partial_groups`` — best truncated/salvaged group list across
      attempts, or ``None`` when nothing could be salvaged.  Caller can
      use this to combine with a local fallback for the missing words.
    """
    prompt = _build_regroup_prompt(words, speakers, target_lang_name)
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 32768,
            "responseMimeType": "application/json",
        },
    }

    last_error = None
    best_partial_groups: list[dict] | None = None

    for key_idx, api_key in enumerate(api_keys):
        try:
            url = f"{GEMINI_API_URL}?key={api_key}"
            async with httpx.AsyncClient(timeout=180.0) as client:
                response = await client.post(url, json=payload)

            if response.status_code in (429, 403):
                logger.warning(
                    "Gemini Key #{} rate-limited (HTTP {}), trying next...",
                    key_idx + 1,
                    response.status_code,
                )
                last_error = f"HTTP {response.status_code}"
                continue

            if response.status_code != 200:
                logger.error(
                    "Gemini regroup API error (HTTP {}): {}",
                    response.status_code,
                    response.text[:300],
                )
                last_error = f"HTTP {response.status_code}"
                continue

            result = response.json()
            candidates = result.get("candidates", [])
            if not candidates:
                logger.warning("Gemini returned no candidates for regrouping")
                last_error = "No candidates"
                continue

            finish_reason = candidates[0].get("finishReason", "")
            is_truncated = finish_reason in ("MAX_TOKENS", "RECITATION")

            content = candidates[0].get("content", {})
            parts = content.get("parts", [])
            raw_text = parts[0].get("text", "") if parts else ""

            # Clean up the raw text to handle common Gemini JSON formatting issues
            raw_text = raw_text.strip()
            if raw_text.startswith("```"):
                raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text)
                raw_text = re.sub(r"\s*```$", "", raw_text)
            raw_text = re.sub(r",(\s*[\]}])", r"\1", raw_text)

            if is_truncated:
                logger.warning(
                    "Gemini Key #{} output truncated (finishReason={}). "
                    "Attempting to salvage partial JSON...",
                    key_idx + 1,
                    finish_reason,
                )
                salvaged = repair_truncated_json(raw_text)
                if salvaged and (
                    not best_partial_groups
                    or len(salvaged) > len(best_partial_groups)
                ):
                    best_partial_groups = salvaged
                last_error = f"Output truncated ({finish_reason})"
                continue

            try:
                groups = json.loads(raw_text)
            except json.JSONDecodeError as parse_exc:
                logger.warning(
                    "Failed to parse Gemini regroup JSON: {} (Snippet: {})",
                    parse_exc,
                    raw_text[max(0, parse_exc.pos - 50):parse_exc.pos + 50]
                    if hasattr(parse_exc, "pos") else raw_text[:200],
                )
                salvaged = repair_truncated_json(raw_text)
                if salvaged and (
                    not best_partial_groups
                    or len(salvaged) > len(best_partial_groups)
                ):
                    best_partial_groups = salvaged
                last_error = f"JSON parse: {parse_exc}"
                continue

            if not isinstance(groups, list) or not groups:
                logger.warning(
                    "Unexpected Gemini regroup response: {}", raw_text[:200],
                )
                last_error = "Unexpected format"
                continue

            return groups, best_partial_groups

        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            logger.warning("Gemini Key #{} network error: {}", key_idx + 1, exc)
            last_error = str(exc)
            continue

    logger.error(
        "All Gemini API keys failed for regrouping. Last error: {}", last_error,
    )
    return None, best_partial_groups
