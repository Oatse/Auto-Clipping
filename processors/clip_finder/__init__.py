"""
processors/clip_finder — YouTube Clip Finder using yt-dlp + Gemini AI.

Package layout (deep modules, single responsibility each):

  orchestrator.py     ClipFinder facade — public API
  subtitle_source.py  yt-dlp subtitle download + cookie matrix
  subtitle_parsers.py json3 / srt / vtt parsers
  transcript.py       merge / condense / slice operations
  heuristics.py       duration hints, VTuber mode, time formatting
  detector.py         single-shot detection + recheck rescue
  hunters.py          single-aspect Hunter runner (multi-stage)
  scoring.py          deterministic features + LLM rubric → ClipScore
  boundary.py         silence-aware start/end snapping
  selection.py        top-N diversified selection
  prompts.py          all prompt builders (detection, recheck, hunter, scoring)
  gemini_client.py    HTTP client with header-auth + key rotation
  audio_signals.py    ffmpeg-based peak + silence extraction
  chat_signals.py     yt-dlp live_chat replay mining
  cache.py            transcript / signals disk cache
  clip_selection.py   JSON salvage, dedup, validation
"""

from .clip_selection import ClipFinderError
from .orchestrator import ClipFinder

__all__ = ["ClipFinder", "ClipFinderError"]
