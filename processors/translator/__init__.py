"""
processors.translator — Phase 2: Translation with duration awareness.

The implementation is split into focused submodules:

* :mod:`.constants`     — language map, Gemini URL, batch-size constants
* :mod:`.gemini_client` — Gemini API calls (translate + regroup) + JSON salvage
* :mod:`.regrouper`     — turn Gemini groups into ``TranscriptSegment``s
* :mod:`.recheck`       — word-level alignment recheck against ElevenLabs
* :mod:`.deepl`         — DeepL fallback translator
* :mod:`.local_grouper` — local heuristic word→subtitle grouper
* :mod:`.processor`     — the :class:`TranslatorProcessor` orchestrator

The class API is unchanged from the legacy single-file module: callers
still ``from processors.translator import TranslatorProcessor``.  The
class also retains the ``recheck_word_level_alignment`` staticmethod
alias used by ``web/server.py``.
"""

from .processor import TranslatorProcessor
from .recheck import recheck_word_level_alignment

__all__ = ["TranslatorProcessor", "recheck_word_level_alignment"]
