"""
processors.elevenlabs_stt — Compatibility shim.

The implementation has moved to :mod:`processors.stt.elevenlabs` as part
of the STT-engine abstraction.  This module keeps the legacy class name
``ElevenLabsSTTProcessor`` as an alias so older import paths
(``from processors.elevenlabs_stt import ElevenLabsSTTProcessor``)
continue to work.

New code should import from :mod:`processors.stt` directly:

    from processors.stt import ElevenLabsSttEngine, SttEngine
"""

from .stt.elevenlabs import API_URL, ElevenLabsSttEngine

# Legacy alias — preserve the old PascalCase + ``Processor`` naming.
ElevenLabsSTTProcessor = ElevenLabsSttEngine

__all__ = ["ElevenLabsSTTProcessor", "ElevenLabsSttEngine", "API_URL"]
