"""
processors.translator.deepl — DeepL Translator fallback.

Used when Gemini translation fails or no Gemini key is configured.
Two flavours:

* :func:`translate_texts` — translate a flat list of strings (used by the
  text-only Gemini fallback path).
* :func:`translate_segments_in_place` — translate segment ``.text`` fields
  in-place (used by the regroup fallback path).

Both functions silently skip and return their input unchanged when
``config.DEEPL_API_KEY`` is not set, so callers don't need to check.
"""

from __future__ import annotations

import httpx
from loguru import logger

import config
from models.transcript import TranscriptSegment


_DEEPL_URL = "https://api-free.deepl.com/v2/translate"
_BATCH_SIZE = 50  # DeepL accepts max 50 strings per request


def _resolve_target_code(target_language: str) -> str:
    """DeepL needs region-qualified codes for English and Portuguese."""
    code = target_language.upper()
    if code == "EN":
        return "EN-US"
    if code == "PT":
        return "PT-PT"
    return code


def _build_headers() -> dict[str, str]:
    return {
        "Authorization": f"DeepL-Auth-Key {config.DEEPL_API_KEY}",
        "Content-Type": "application/json",
    }


async def translate_texts(
    texts: list[str],
    target_language: str,
) -> list[str]:
    """Translate a list of strings via DeepL.

    Returns the original ``texts`` unchanged when no key is set or all
    DeepL calls fail.  Never raises.
    """
    if not texts:
        return texts

    if not config.DEEPL_API_KEY:
        logger.warning(
            "DeepL fallback skipped: DEEPL_API_KEY not configured. "
            "Returning source-language texts as-is."
        )
        return texts

    logger.info(
        "Starting DeepL fallback for text-only translation ({} items)...",
        len(texts),
    )

    target_code = _resolve_target_code(target_language)
    headers = _build_headers()

    result_texts = list(texts)

    for i in range(0, len(texts), _BATCH_SIZE):
        batch_texts = texts[i:i + _BATCH_SIZE]
        payload = {"text": batch_texts, "target_lang": target_code}

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(_DEEPL_URL, headers=headers, json=payload)

            if response.status_code == 200:
                data = response.json()
                translations = data.get("translations", [])
                for j, translated_item in enumerate(translations):
                    index_in_results = i + j
                    if index_in_results < len(result_texts):
                        result_texts[index_in_results] = translated_item.get(
                            "text", batch_texts[j]
                        )
            else:
                logger.error(
                    "DeepL text-only API error (HTTP {}): {}",
                    response.status_code,
                    response.text[:300],
                )
        except Exception as exc:  # noqa: BLE001 — keep the fallback alive
            logger.error("Network error during DeepL fallback (texts): {}", exc)

    return result_texts


async def translate_segments_in_place(
    segments: list[TranscriptSegment],
    target_language: str,
) -> list[TranscriptSegment]:
    """Translate each segment's ``text`` field in-place via DeepL.

    Returns ``segments`` unchanged when no key is set.  Mutates the
    objects in-place — also returns the same list for chaining.
    """
    if not segments:
        return segments

    if not config.DEEPL_API_KEY:
        logger.warning(
            "DeepL fallback skipped: DEEPL_API_KEY not configured. "
            "Segments will be returned with their source-language text."
        )
        return segments

    logger.info(
        "Starting DeepL fallback translation for {} segments...",
        len(segments),
    )

    target_code = _resolve_target_code(target_language)
    headers = _build_headers()

    for i in range(0, len(segments), _BATCH_SIZE):
        batch = segments[i:i + _BATCH_SIZE]
        texts_to_translate = [seg.text for seg in batch]
        payload = {"text": texts_to_translate, "target_lang": target_code}

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(_DEEPL_URL, headers=headers, json=payload)

            if response.status_code == 200:
                data = response.json()
                translations = data.get("translations", [])
                for j, translated_item in enumerate(translations):
                    if j < len(batch):
                        batch[j].text = translated_item.get("text", batch[j].text)
            else:
                logger.error(
                    "DeepL API error (HTTP {}): {}",
                    response.status_code,
                    response.text[:300],
                )
        except Exception as exc:  # noqa: BLE001 — keep the fallback alive
            logger.error("Network error during DeepL fallback: {}", exc)

    return segments
