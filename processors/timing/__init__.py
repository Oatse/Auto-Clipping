"""
processors.timing — Centralized subtitle timing policy + sanitization passes.

This package is the **single seam** for everything that adjusts word- or
segment-level timestamps after STT.  Three callers used to mutate
timestamps independently with different assumptions:

    1. ``models.transcript.sanitize_timestamps``       (4 passes)
    2. ``processors.translator.recheck_word_level_alignment`` (9 passes)
    3. ``web.server._sync_segment_words_with_text``    (proportional reflow)

Mixing them produced the timing drift documented in the audit report.
The :class:`Sanitizer` exposed here is the canonical replacement.  The
older entrypoints are kept as compat shims that delegate to this module
so old callers keep working while new code uses
``Sanitizer(policy).sanitize(...)`` directly.

Public API
----------

>>> from processors.timing import Sanitizer, TimingPolicy
>>> sanitizer = Sanitizer(TimingPolicy())
>>> sanitizer.sanitize(segments)            # word + segment passes
>>> sanitizer.sanitize_segment_only(segs)   # skip word-level passes
"""

from .policy import TimingPolicy
from .sanitizer import Sanitizer

__all__ = ["TimingPolicy", "Sanitizer"]
