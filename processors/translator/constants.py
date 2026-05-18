"""
processors.translator.constants — Static configuration shared by all
translator submodules.
"""

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

# Gemini endpoint used for both batch translation and regrouping calls.
GEMINI_API_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-3-flash-preview:generateContent"
)

# Default text-only translate batch size — tuned to stay under Gemini's
# 8K output token budget for short subtitle lines.
BATCH_SIZE = 30

# Regex matching strings consisting only of punctuation/quote characters.
# Stored as a string so ``re.fullmatch`` can compile it on demand.
PUNCTUATION_ONLY_PATTERN = r'[\s\.\!\?\,\;\:\-\—\–\…\"\'«»""'']+'
