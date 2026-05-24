"""
processors.translator.constants — Static configuration shared by all
translator submodules.
"""

import config


# Language code → human-readable name (used in Gemini prompts).
LANGUAGE_NAMES: dict[str, str] = {
    "en": "English",
    "id": "Indonesian",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "ja": "Japanese",
    "ko": "Korean",
    "zh": "Chinese",
    "ar": "Arabic",
    "pt": "Portuguese",
    "ru": "Russian",
    "hi": "Hindi",
    "th": "Thai",
    "vi": "Vietnamese",
    "tr": "Turkish",
    "it": "Italian",
    "nl": "Dutch",
    "pl": "Polish",
}

# Gemini API base URL (model + ":generateContent" appended at call site).
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"


def gemini_url(model: str) -> str:
    """Build full Gemini generateContent URL for a model."""
    return f"{GEMINI_API_BASE}/{model}:generateContent"


def translator_models() -> list[str]:
    """Ordered model list for the translator: primary then fallbacks."""
    primary = config.TRANSLATOR_GEMINI_MODEL
    fallbacks = config.TRANSLATOR_GEMINI_FALLBACK_MODELS
    seen: set[str] = set()
    out: list[str] = []
    for m in [primary, *fallbacks]:
        if m and m not in seen:
            out.append(m)
            seen.add(m)
    return out


# Legacy alias kept so old imports keep working — points at the primary model.
GEMINI_API_URL = gemini_url(config.TRANSLATOR_GEMINI_MODEL)


# Default text-only translate batch size — tuned to stay under Gemini's
# 8K output token budget for short subtitle lines.
BATCH_SIZE = 30

# Regex matching strings consisting only of punctuation/quote characters.
# Includes straight quotes, smart curly quotes (U+2018-U+201D), guillemets
# («»), em/en dashes, and the horizontal ellipsis. Stored as a string so
# ``re.fullmatch`` can compile it on demand.
PUNCTUATION_ONLY_PATTERN = (
    r"[\s.!?,;:\-\u2010-\u2015\u2026"          # ASCII + dashes + ellipsis
    r"\"'\u2018\u2019\u201C\u201D\u00AB\u00BB" # straight + curly + guillemets
    r"]+"
)

