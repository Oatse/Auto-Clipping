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

from .constants import GEMINI_SEED, gemini_url, translator_models


# ── Anti-AI-translate baseline ─────────────────────────────────────────────
#
# Single source of truth for the "don't sound like a translator bot" rules.
# Sent as ``systemInstruction`` on every Gemini call so the per-batch user
# message can stay focused on the data + task. Update PROMPT_VERSION in
# constants.py whenever this changes.
_SYSTEM_INSTRUCTION_BASELINE = (
    "You are a professional subtitle translator for short-form video clips. "
    "Your output is read by viewers in real time, on a small screen, with "
    "limited attention. Your job is to make the translation feel like a "
    "fluent human wrote it — never literal, never robotic, never generic.\n"
    "\n"
    "Hard rules — apply on every line:\n"
    "1. Translate idioms FUNCTIONALLY, never literally. If the source says "
    "something like 'I cannot accept that', the target must convey the "
    "speaker's actual feeling (disagreement, doubt) using a phrase a native "
    "speaker would actually say in the target language.\n"
    "2. Word choice must be CONTEXT-AWARE. Pay attention to register, "
    "speaker relationship, content domain (gaming, podcast, lecture, vlog) "
    "and pick the term a native speaker would use in that exact context, "
    "not the dictionary-first equivalent.\n"
    "3. Light, natural fillers and hedges in the target language are "
    "ALLOWED when they make the line feel spoken (e.g. 'kind of', "
    "'actually', 'you know', 'ya', 'gitu', 'sih'). Heavy internet slang "
    "is NOT allowed (e.g. 'fr', 'ngl', 'lol', 'anjir', 'wkwk') unless the "
    "user's style note explicitly opts in.\n"
    "4. RESTRUCTURE across short fragmented lines when a literal "
    "line-by-line rendering would feel choppy in the target language. "
    "Merge or split phrasing so the result reads naturally — but keep "
    "the segment-to-segment ordering and total count unchanged.\n"
    "5. Do NOT use trailing ellipses ('...') as a default placeholder for "
    "speech pauses. Only keep them when the speaker is genuinely "
    "trailing off or being dramatic.\n"
    "6. Preserve emotional elongation (e.g. 'noooooo', 'BAKAAAAAA', "
    "'stoppppp') by using the equivalent stretched form in the target "
    "language.\n"
    "7. Lines that consist entirely of expressive vocalizations, "
    "onomatopoeia, or romanized non-source-language sounds with no "
    "lexical meaning must be kept EXACTLY as-is. Examples: 'kyaaaa', "
    "'uwaaaa', 'ahahaha', 'ufufufu', 'zuuun', 'dodododo', 'buwakkushun'. "
    "Do NOT localize these to the target language's onomatopoeia "
    "(e.g. 'buwakkushun' must NOT become 'Achoo'; 'kyaaaa' must NOT "
    "become 'eeeeek').\n"
    "8. Never invent content the speaker did not say. Tone and phrasing "
    "may be adapted; meaning and intent must not change.\n"
    "9. REGISTER MATCHING. Match the formality of the source line. "
    "If the source uses everyday words, your translation must too. "
    "Avoid elevated, literary, or rare vocabulary unless the source "
    "is genuinely formal. Prefer 'amazing' over 'magnificent', "
    "'great' over 'splendid', 'really' over 'truly', 'a lot' over "
    "'a great deal', 'sleep position' over 'sleeping posture'.\n"
    "10. LENGTH PROPORTIONALITY. If the source line is short and "
    "elliptical, the translation must be short too. Do NOT pad short "
    "fragments with explanatory clauses, framing phrases, or hedging. "
    "If the source has 3 words, do not output 12. Mirror the brevity.\n"
    "11. COMMUNITY / GENRE CONVENTIONS. When translating fan-content "
    "(VTuber clips, gaming streams, anime/manga discussion), prefer "
    "the term the fan community actually uses in the target language "
    "over the dictionary-first equivalent. In particular:\n"
    "   - Keep Japanese honorifics RAW: -senpai, -chan, -san, -kun, "
    "-sama, -dono. Do NOT translate to 'elder', 'senior', 'mister'. "
    "Same policy applies to 'shishou' (master/teacher in fan context "
    "— keep raw).\n"
    "   - Use established fan-translations for common terms: "
    "egosa/エゴサ → 'ego-search' (NOT 'vanity search'); "
    "haishin/配信 → 'stream' (NOT 'broadcast'); "
    "oshi/推し → 'oshi' (kept raw); "
    "live/ライブ → 'live show' or 'concert' depending on scale.\n"
    "   - For gaming context, use the gameplay term, not the "
    "dictionary term: shougai/障害 in a game context → 'handicap' "
    "(NOT 'obstacle' or 'sabotage').\n"
    "12. CONTEXT-DRIVEN WORD SENSE. When a source word has multiple "
    "target-language equivalents, pick the one that best fits the "
    "immediate emotional/relational context, not the most common "
    "dictionary entry. Example: 親しみ in a friendship context → "
    "'closeness' (NOT 'relatability').\n"
)

_PRESET_NATURAL = (
    "Style preset: NATURAL.\n"
    "Tone of a casual but competent narrator. Contractions OK. "
    "Conversational connectors and light hedges OK. The line should "
    "sound like a real person talking to a friend, not a press release "
    "and not a tweet."
)

_PRESET_FORMAL = (
    "Style preset: FORMAL.\n"
    "Full sentences, no contractions, neutral register. Suitable for "
    "lectures, news, corporate, and educational content. Still natural — "
    "not stiff — but precise and professional."
)

_PRESET_BLOCKS: dict[str, str] = {
    "natural": _PRESET_NATURAL,
    "formal": _PRESET_FORMAL,
}


# ── Spicy filter (soft R18 censor) ─────────────────────────────────────────
#
# Opt-in instruction block that softens explicit sexual / vulgar source
# language into playful equivalents instead of literal vulgar English.
# Mirrors the behaviour fan-sub groups apply in their own subtitles —
# preserve the joke / tone, drop the harshness one notch.
#
# Pairs with the post-processing pass in
# :mod:`processors.translator.postprocess` which catches cases where the
# model ignored these instructions and translated literally anyway.
_SPICY_FILTER_BLOCK = (
    "Spicy filter: ON.\n"
    "When the source contains explicit sexual / vulgar vocabulary, "
    "translate it using PLAYFUL, softer equivalents instead of literal "
    "vulgar English. Keep the joke and the speaker's tone — just round "
    "the harshness down one notch. Examples:\n"
    "  - 'ochinchin' / 'chinpo' / 'chinchin' → 'wiener' (NOT 'dick' / 'cock')\n"
    "  - 'manko' / 'omanko' → 'lady-bits' (NOT 'pussy')\n"
    "  - 'iku' / 'iku!' (sexual climax) → 'I'm finishing' "
    "(NOT 'I'm cumming')\n"
    "  - 'ecchi' / 'eroi' / 'sukebe' → 'naughty' or 'spicy' "
    "(NOT 'horny' / 'lewd' / 'perv')\n"
    "  - 'oppai' (when explicit) → 'boobies' (NOT 'tits')\n"
    "  - 'shimoneta' / dirty-joke vocabulary → keep the joke shape but "
    "swap the explicit noun for the playful one.\n"
    "Hard rule: NEVER output 'dick', 'cock', 'pussy', 'tits', 'cum', "
    "'cumming', 'horny', 'slut'. Use the playful map above instead. "
    "Mild profanity ('damn', 'crap', 'hell') stays as-is.\n"
    "If the source has no explicit content, this block has zero effect."
)


def _build_system_instruction(
    target_lang_name: str,
    style_preset: str = "natural",
    style_note: str | None = None,
    spicy_filter: bool = False,
) -> str:
    """Compose the Gemini systemInstruction for a translation call.

    The preset block is appended to the immutable baseline. ``style_note``
    is an optional, free-form user instruction that is appended ADDITIVELY
    after a clear delimiter — it never replaces the preset.

    When ``spicy_filter`` is True, an extra block instructs the model to
    soften explicit R18 vocabulary into playful equivalents instead of
    literal vulgar English (defense layer 1). The post-processing pass
    in :mod:`processors.translator.postprocess` is layer 2.
    """
    preset_block = _PRESET_BLOCKS.get(
        style_preset.lower(), _PRESET_NATURAL,
    )
    parts = [
        _SYSTEM_INSTRUCTION_BASELINE,
        f"Target language: {target_lang_name}.",
        preset_block,
    ]
    if spicy_filter:
        parts.append(_SPICY_FILTER_BLOCK)
    if style_note and style_note.strip():
        parts.append(
            "--- Additional user style note ---\n"
            f"{style_note.strip()}\n"
            "--- End user style note ---"
        )
    return "\n\n".join(parts)


def _build_translate_prompt(texts: list[str], target_lang_name: str) -> str:
    """Build the per-batch user message for plain-text translation.

    The anti-AI baseline + style preset live in ``systemInstruction``; this
    string only carries the data + the minimal task framing.
    """
    numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(texts))
    return (
        f"Translate the following {len(texts)} numbered subtitle lines into "
        f"{target_lang_name}. Apply the style and anti-AI rules from your "
        "system instruction.\n\n"
        "Return ONLY a JSON array of translated strings in the same order, "
        "with no numbering and no extra prose.\n\n"
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
        "You will GROUP a list of transcribed words into subtitle segments "
        "AND translate each group. Apply the style and anti-AI rules from "
        "your system instruction.\n\n"
        "Below is a numbered list of transcribed words from a video.\n"
        "[PAUSE Xs] markers indicate silence gaps between words.\n"
    )
    if multi_speaker:
        prompt += "[SPEAKER_XX] tags indicate speaker changes.\n"
    prompt += (
        "\nGrouping rules:\n"
        "- Maximum 12 words per subtitle\n"
        "- Break at sentence boundaries (., !, ?, 。, ！, ？) when possible\n"
        "- ALWAYS start a new subtitle at [PAUSE] markers\n"
    )
    if multi_speaker:
        prompt += "- ALWAYS start a new subtitle on speaker changes\n"
    prompt += (
        "- Keep each subtitle as a complete phrase or sentence\n"
        "- NEVER create a group whose translation is only punctuation. "
        "Always include at least one meaningful word\n"
        "- If consecutive words look like fragments of one word "
        "(e.g. 'beau' + 'tiful' = 'beautiful'), treat them as a single "
        "word in your translation.\n\n"
        f"Translate each group into {target_lang_name}.\n\n"
        f"Words:\n{word_list_text}\n\n"
        "Return ONLY a strictly valid JSON array where each element is:\n"
        '{"indices": [0, 1, 2], "translated": "translated subtitle text"}\n\n'
        "CRITICAL RULES:\n"
        "- Keep your response concise — short, natural subtitle translations\n"
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
    style_preset: str = "natural",
    style_note: str | None = None,
    spicy_filter: bool = False,
) -> list[str] | None:
    """Translate a batch of plain texts via Gemini.

    Rotates keys on rate-limit/error.  Returns the translated list or
    ``None`` when every key failed (caller should invoke the DeepL fallback).
    """
    prompt = _build_translate_prompt(texts, target_lang_name)
    system_instruction = _build_system_instruction(
        target_lang_name, style_preset, style_note,
        spicy_filter=spicy_filter,
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "systemInstruction": {"parts": [{"text": system_instruction}]},
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 8192,
            "responseMimeType": "application/json",
            "seed": GEMINI_SEED,
        },
    }

    last_error = None
    models = translator_models()
    for model in models:
        url = gemini_url(model)
        for key_idx, api_key in enumerate(api_keys):
            try:
                async with httpx.AsyncClient(timeout=120.0) as client:
                    response = await client.post(
                        url,
                        json=payload,
                        headers={"x-goog-api-key": api_key},
                    )

                if response.status_code in (429, 403):
                    logger.warning(
                        "Gemini translate model={} key#{} rate-limited (HTTP {}), trying next key...",
                        model, key_idx + 1, response.status_code,
                    )
                    last_error = f"HTTP {response.status_code}"
                    continue

                if response.status_code == 404:
                    logger.warning(
                        "Gemini model '{}' not available (HTTP 404) — falling back to next model",
                        model,
                    )
                    last_error = f"Model '{model}' not found"
                    break  # break key loop, try next model

                if response.status_code >= 500:
                    logger.warning(
                        "Gemini translate model={} key#{} server error (HTTP {}), trying next key...",
                        model, key_idx + 1, response.status_code,
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
                logger.warning(
                    "Gemini translate model={} key#{} network error: {}",
                    model, key_idx + 1, exc,
                )
                last_error = str(exc)
                continue

    logger.error(
        "All Gemini API keys/models failed for translation. Last error: {}", last_error,
    )
    return None


async def call_gemini_regroup(
    words: list[WordTimestamp],
    speakers: list[str],
    target_lang_name: str,
    api_keys: list[str],
    style_preset: str = "natural",
    style_note: str | None = None,
    spicy_filter: bool = False,
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
    system_instruction = _build_system_instruction(
        target_lang_name, style_preset, style_note,
        spicy_filter=spicy_filter,
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "systemInstruction": {"parts": [{"text": system_instruction}]},
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 32768,
            "responseMimeType": "application/json",
            "seed": GEMINI_SEED,
        },
    }

    last_error = None
    best_partial_groups: list[dict] | None = None

    models = translator_models()
    for model in models:
        url = gemini_url(model)
        for key_idx, api_key in enumerate(api_keys):
            try:
                async with httpx.AsyncClient(timeout=180.0) as client:
                    response = await client.post(
                        url,
                        json=payload,
                        headers={"x-goog-api-key": api_key},
                    )

                if response.status_code in (429, 403):
                    logger.warning(
                        "Gemini regroup model={} key#{} rate-limited (HTTP {}), trying next key...",
                        model, key_idx + 1, response.status_code,
                    )
                    last_error = f"HTTP {response.status_code}"
                    continue

                if response.status_code == 404:
                    logger.warning(
                        "Gemini regroup model '{}' not available (HTTP 404) — trying next model",
                        model,
                    )
                    last_error = f"Model '{model}' not found"
                    break

                if response.status_code >= 500:
                    logger.warning(
                        "Gemini regroup model={} key#{} server error (HTTP {}), trying next key...",
                        model, key_idx + 1, response.status_code,
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
                        "Gemini regroup model={} key#{} output truncated (finishReason={}). "
                        "Attempting to salvage partial JSON...",
                        model, key_idx + 1, finish_reason,
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
                logger.warning(
                    "Gemini regroup model={} key#{} network error: {}",
                    model, key_idx + 1, exc,
                )
                last_error = str(exc)
                continue

    logger.error(
        "All Gemini API keys failed for regrouping. Last error: {}", last_error,
    )
    return None, best_partial_groups
