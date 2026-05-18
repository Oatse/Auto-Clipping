"""
processors.timing.policy — Tuning knobs for the timing sanitizer.

All magic numbers that used to be scattered across
``models.transcript`` and ``processors.translator`` live here so they
can be tuned in one place and shared by every caller.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TimingPolicy:
    """Tuning knobs for word/segment timing sanitization.

    The defaults match the behaviour after the P0 audit fixes — they are
    tuned for ElevenLabs Scribe v1 output on talk-show / anime audio.

    Attributes
    ----------
    silence_cap:
        Maximum tolerated silence (seconds) appended to a word's
        estimated duration before its ``end`` is considered broken.
        ``2.0`` keeps a wide safety margin so only clearly-broken
        timestamps get capped.
    char_to_seconds:
        Per-character estimate added on top of ``base_seconds`` when
        modelling normal spoken duration.
    base_seconds:
        Floor estimate for any word.
    duration_min:
        Lower bound on the duration estimate (a 1-letter word still gets
        at least this long).
    duration_max:
        Upper bound on the duration estimate **for normal words**.
        Elongated words bypass this clamp via ``elongation_per_char``.
    elongation_run_threshold:
        Minimum length of a run of identical characters that flags a
        word as "emotionally elongated" (e.g. ``noooo``).  ``3`` matches
        common anime / vtuber pronunciation.
    elongation_per_char:
        Extra duration budget per repeated character on elongated words.
    speech_rate_factor_min:
        Multiplier on the duration estimate is never tightened below
        this floor (= keep the original cap when audio is normal pace).
    speech_rate_factor_max:
        Cap on the auto-loosen factor so a single corrupt segment
        cannot blow the cap up unboundedly.
    speech_rate_log_threshold:
        Multipliers below this don't get logged (avoids noise).
    same_speaker_segment_gap:
        Minimum gap (seconds) inserted between two same-speaker segments
        when an overlap is trimmed.  10 ms keeps them visually distinct
        without being perceptible.
    minimum_segment_duration:
        When a same-speaker overlap leaves no room, the earlier segment
        is forced to at least this duration starting at its original
        ``start``.
    """

    # Word-duration cap
    silence_cap: float = 2.0
    char_to_seconds: float = 0.09
    base_seconds: float = 0.15
    duration_min: float = 0.3
    duration_max: float = 1.5

    # Elongation handling
    elongation_run_threshold: int = 3
    elongation_per_char: float = 0.35

    # Adaptive speech rate
    speech_rate_factor_min: float = 1.0
    speech_rate_factor_max: float = 3.0
    speech_rate_log_threshold: float = 1.05

    # Segment-level
    same_speaker_segment_gap: float = 0.01
    minimum_segment_duration: float = 0.05
