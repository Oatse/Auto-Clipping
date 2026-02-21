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
# HF_TOKEN is optional — only needed for speaker diarization (pyannote).
# If not set, all transcript segments are assigned to SPEAKER_00.
HF_TOKEN: str = os.getenv("HF_TOKEN", "")

# GEMINI_API_KEYS — required for Clip Finder (AI-based clip detection).
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


# ─── yt-dlp ──────────────────────────────────────────────────────────────────
YTDLP_PATH: str = os.getenv("YTDLP_PATH", "./bin/yt-dlp.exe")
YTDLP_COOKIES_BROWSER: str = os.getenv("YTDLP_COOKIES_BROWSER", "")  # e.g. "edge", "chrome", "firefox"
YTDLP_COOKIES_FILE: str = os.getenv("YTDLP_COOKIES_FILE", "")        # path to cookies.txt file (takes priority over browser)


# ─── WhisperX ─────────────────────────────────────────────────────────────────
WHISPERX_MODEL: str = os.getenv("WHISPERX_MODEL", "large-v2")
WHISPERX_COMPUTE_TYPE: str = os.getenv("WHISPERX_COMPUTE_TYPE", "float16")
WHISPERX_DEVICE: str = os.getenv("WHISPERX_DEVICE", "cuda")
WHISPERX_BATCH_SIZE: int = int(os.getenv("WHISPERX_BATCH_SIZE", "16"))
WHISPERX_LANGUAGE: str = os.getenv("WHISPERX_LANGUAGE", "en")

# ─── Whisper Model Options ────────────────────────────────────────────────────
# Available transcription models. Each entry defines:
#   - label: Display name shown in the UI
#   - type: "whisperx" (standard) or "faster-whisper" (local model)
#   - model: Model name/size (for whisperx) or local path (for faster-whisper)
#   - description: Short description for the UI
WHISPER_ANIME_MODEL_PATH: str = os.getenv(
    "WHISPER_ANIME_MODEL_PATH",
    str(BASE_DIR / "models" / "whisper-anime")
)

WHISPER_MODELS: dict = {
    "large-v2": {
        "label": "WhisperX Large-V2",
        "type": "whisperx",
        "model": "large-v2",
        "description": "Standard model — best for general content (English, multilingual)",
    },
    "large-v3": {
        "label": "WhisperX Large-V3",
        "type": "whisperx",
        "model": "large-v3",
        "description": "Latest standard model — improved accuracy",
    },
    "anime": {
        "label": "Anime Whisper",
        "type": "faster-whisper",
        "model": WHISPER_ANIME_MODEL_PATH,
        "description": "Optimized for anime/manga content — better for Japanese audio",
    },
    "elevenlabs": {
        "label": "ElevenLabs Speech-to-Text",
        "type": "elevenlabs",
        "model": "elevenlabs",
        "description": "Cloud-based STT — auto-translate via Gemini to target language",
    },
}


# ─── FFmpeg ───────────────────────────────────────────────────────────────────
FFMPEG_PATH: str = os.getenv("FFMPEG_PATH", "ffmpeg")
FFPROBE_PATH: str = os.getenv("FFPROBE_PATH", "ffprobe")


# ─── Audio Settings ───────────────────────────────────────────────────────────
AUDIO_SAMPLE_RATE: int = 44100
AUDIO_CHANNELS: int = 1  # Mono for speech
