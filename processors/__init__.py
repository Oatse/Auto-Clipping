"""processors package — STT, translation, subtitle rendering, muxing."""
from .stt import ElevenLabsSttEngine, SttEngine
from .translator import TranslatorProcessor
from .subtitle_renderer import SubtitleRendererProcessor
from .muxer import MuxerProcessor

# Legacy alias — see processors/elevenlabs_stt.py for the compat shim.
ElevenLabsSTTProcessor = ElevenLabsSttEngine

__all__ = [
    "ElevenLabsSttEngine",
    "ElevenLabsSTTProcessor",  # legacy alias
    "SttEngine",
    "TranslatorProcessor",
    "SubtitleRendererProcessor",
    "MuxerProcessor",
]
