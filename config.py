"""
config.py — Centralized configuration for the Video Clip Automation System.
All values are loaded from environment variables (via .env file).
"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


# ─── Project Paths ────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.resolve()
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "./output")).resolve()
TEMP_DIR = Path(os.getenv("TEMP_DIR", "./output/temp")).resolve()

# Ensure directories exist at import time
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
TEMP_DIR.mkdir(parents=True, exist_ok=True)


# ─── API Keys ─────────────────────────────────────────────────────────────────
# GEMINI_API_KEYS — required for translation/regrouping and Clip Finder.
# Supports multiple keys for automatic fallback when a key is rate-limited.
GEMINI_API_KEYS: list[str] = [
    k for k in [
        os.getenv("GEMINI_API_KEY_01", ""),
        os.getenv("GEMINI_API_KEY_02", ""),
        os.getenv("GEMINI_API_KEY_03", ""),
        os.getenv("GEMINI_API_KEY", ""),          # legacy single-key fallback
    ] if k
]

# ELEVENLABS_API_KEYS — for ElevenLabs Speech-to-Text transcription.
# Supports multiple keys for automatic fallback when a key is rate-limited or exhausted.
# Use ELEVENLABS_API_KEY_01 / _02 for multi-key, or legacy ELEVENLABS_API_KEY.
ELEVENLABS_API_KEYS: list[str] = [
    k for k in [
        os.getenv("ELEVENLABS_API_KEY_01", ""),   # explicit primary (new format)
        os.getenv("ELEVENLABS_API_KEY", ""),       # legacy single key (primary if _01 not set)
        os.getenv("ELEVENLABS_API_KEY_02", ""),   # fallback / secondary
        os.getenv("ELEVENLABS_API_KEY_03", ""),   # fallback / tertiary
    ] if k
]
# Backward-compatible single-key alias (first available key)
ELEVENLABS_API_KEY: str = ELEVENLABS_API_KEYS[0] if ELEVENLABS_API_KEYS else ""

# DEEPL_API_KEY — fallback translator when Gemini fails or has no key.
# When unset, the DeepL fallback is silently skipped and the source-language
# text is returned as-is (so subtitles still render, just untranslated).
# The free tier suffix ":fx" is required by DeepL — keep it on the key.
DEEPL_API_KEY: str = os.getenv("DEEPL_API_KEY", "")


# ─── ElevenLabs Scribe Tuning ────────────────────────────────────────────────
# Model selection. ``scribe_v2`` is the current state-of-the-art model and
# is required for ``no_verbatim``. Override to ``scribe_v1`` only when
# pinning to legacy behaviour during a regression investigation.
ELEVENLABS_STT_MODEL: str = os.getenv("ELEVENLABS_STT_MODEL", "scribe_v2")

# When True, the STT response will have filler words, false starts, and
# disfluencies removed by the model itself. NOTE (2026-05-28 audit): in
# our Japanese sample the effective text length actually grew slightly,
# suggesting the feature is English-first today. Default OFF; turn on
# per-deployment if your content is English-heavy.
ELEVENLABS_NO_VERBATIM: bool = (
    os.getenv("ELEVENLABS_NO_VERBATIM", "false").lower() == "true"
)

# Sampling temperature passed to Scribe. 0.0 makes the response as
# deterministic as the model allows, which makes re-runs reproducible
# (handy for regression testing the rest of the pipeline).
ELEVENLABS_STT_TEMPERATURE: float = float(
    os.getenv("ELEVENLABS_STT_TEMPERATURE", "0")
)

# Seed paired with ``temperature=0`` to make repeat transcriptions of
# the same audio return identical results.  Empty string → omit the
# field from the payload (let Scribe pick its own seed).
ELEVENLABS_STT_SEED: str = os.getenv("ELEVENLABS_STT_SEED", "42")


# ─── yt-dlp ──────────────────────────────────────────────────────────────────
YTDLP_COOKIES_BROWSER: str = os.getenv("YTDLP_COOKIES_BROWSER", "")  # e.g. "edge", "chrome", "firefox"
_default_cookies_file = os.path.join(os.path.dirname(__file__), "cookies.txt")
YTDLP_COOKIES_FILE: str = os.getenv(
    "YTDLP_COOKIES_FILE",
    _default_cookies_file if os.path.isfile(_default_cookies_file) else "",
)  # path to cookies.txt file (takes priority over browser)


# ─── Gemini Models ───────────────────────────────────────────────────────────
# Gemini model used for translation + word regrouping in Phase 2.
# Override by setting TRANSLATOR_GEMINI_MODEL in .env (e.g. "gemini-2.5-flash"
# if the preview model is not available on your project).
TRANSLATOR_GEMINI_MODEL: str = os.getenv(
    "TRANSLATOR_GEMINI_MODEL",
    "gemini-3.5-flash",
)
# Fallback chain for translator when the primary model is deprecated / regional.
# Tried in order; first one that returns 200 wins.
TRANSLATOR_GEMINI_FALLBACK_MODELS: list[str] = [
    m.strip() for m in os.getenv(
        "TRANSLATOR_GEMINI_FALLBACK_MODELS",
        "gemini-3.5-flash,gemini-2.5-flash",
    ).split(",") if m.strip()
]


# ─── Clip Finder ─────────────────────────────────────────────────────────────
# Gemini model used for clip detection / scoring. Override by setting
# CLIP_FINDER_GEMINI_MODEL in .env (e.g. "gemini-2.5-flash" if 3-flash-preview
# is not available on your project).
CLIP_FINDER_GEMINI_MODEL: str = os.getenv(
    "CLIP_FINDER_GEMINI_MODEL",
    "gemini-3.5-flash",
)
# Fallback chain for clip-finder when primary model unavailable.
CLIP_FINDER_GEMINI_FALLBACK_MODELS: list[str] = [
    m.strip() for m in os.getenv(
        "CLIP_FINDER_GEMINI_FALLBACK_MODELS",
        "gemini-3.5-flash,gemini-2.5-flash",
    ).split(",") if m.strip()
]

# Default detection mode. "single-shot" = legacy 1-prompt path. "multi-stage"
# enables the Hunters → Score → Refine → Diversify pipeline (higher quality
# but uses more Gemini quota — multiple LLM calls per request).
CLIP_FINDER_MODE: str = os.getenv("CLIP_FINDER_MODE", "single-shot")

# Maximum clips returned by multi-stage selection. Single-shot ignores this.
CLIP_FINDER_MAX_CLIPS: int = int(os.getenv("CLIP_FINDER_MAX_CLIPS", "12"))

# Multimodal signal extraction toggles. Audio analysis adds a brief audio-only
# download + ffmpeg pass; chat analysis is free for any video with chat replay.
CLIP_FINDER_ENABLE_AUDIO_SIGNALS: bool = (
    os.getenv("CLIP_FINDER_ENABLE_AUDIO_SIGNALS", "true").lower() == "true"
)
CLIP_FINDER_ENABLE_CHAT_SIGNALS: bool = (
    os.getenv("CLIP_FINDER_ENABLE_CHAT_SIGNALS", "true").lower() == "true"
)

# Where the transcript / signals cache lives (avoids re-extraction on retries).
CLIP_FINDER_CACHE_DIR: Path = Path(
    os.getenv("CLIP_FINDER_CACHE_DIR", str(OUTPUT_DIR / "clip_finder_cache"))
)
CLIP_FINDER_CACHE_DIR.mkdir(parents=True, exist_ok=True)


# ─── FFmpeg ───────────────────────────────────────────────────────────────────
FFMPEG_PATH: str = os.getenv("FFMPEG_PATH", "ffmpeg")
FFPROBE_PATH: str = os.getenv("FFPROBE_PATH", "ffprobe")


# ─── Audio Settings ───────────────────────────────────────────────────────────
AUDIO_SAMPLE_RATE: int = 44100
AUDIO_CHANNELS: int = 1  # Mono for speech


# ─── Web Upload Limits ────────────────────────────────────────────────────────
# Maximum allowed video upload size (bytes).  Default 4 GiB.  Override via
# .env when serving from constrained hosts (e.g. shared VPS) or relax for
# self-hosted batch jobs.
MAX_UPLOAD_BYTES: int = int(
    os.getenv("MAX_UPLOAD_BYTES", str(4 * 1024 * 1024 * 1024))
)
# Chunk size for streaming uploads to disk (bytes).  1 MiB balances syscall
# overhead vs. memory pressure for many concurrent uploads.
UPLOAD_CHUNK_BYTES: int = int(os.getenv("UPLOAD_CHUNK_BYTES", str(1024 * 1024)))

