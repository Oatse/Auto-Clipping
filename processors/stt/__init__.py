"""
processors.stt — Speech-to-Text engine abstraction.

Single-engine deployment today (ElevenLabs Cloud STT), but the package
exposes a :class:`SttEngine` Protocol so additional engines (e.g. local
Whisper, Azure, AssemblyAI) can be plugged in without changing
orchestration code.

Public API
----------

>>> from processors.stt import SttEngine, ElevenLabsSttEngine
>>> engine: SttEngine = ElevenLabsSttEngine()
>>> segments, json_path = await engine.transcribe(video_path, output_dir)
"""

from .base import SttEngine
from .elevenlabs import ElevenLabsSttEngine

__all__ = ["SttEngine", "ElevenLabsSttEngine"]
