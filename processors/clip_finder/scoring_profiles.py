"""
processors/clip_finder/scoring_profiles.py — Named bundles of ClipScore weights.

A Scoring Profile is a content-style preset that re-weights how the
existing five LLM rubric dimensions and three deterministic features
combine into a single ``ClipScore.total``.  The profile only changes
ranking; it never changes detection, boundary refinement, or rendering.

Why this lives in a separate module rather than inline on ``ClipScore``:

* The default weights table on ``ClipScore.total`` was tuned for VTuber
  livestream content. Podcasts, news, ASMR, and gaming each reward a
  different combination of the same dimensions. Hard-coding the VTuber
  bias inside the dataclass made other niches under-score.
* Adding a new profile is a one-line dict entry here, not a ``ClipScore``
  surgery. The ``ClipScore`` shape stays stable — important because it
  ships across the wire to the UI as ``score.to_dict()``.

CONTEXT.md: see "Scoring Profile" definition.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class ScoringProfile(str, Enum):
    """Named scoring profile selected per Job.

    Stored on ``Job.scoring_profile`` (or ``AllInJob.scoring_profile``)
    so re-runs are reproducible.  Defaults to ``VTUBER`` everywhere so
    pre-existing callers see no behaviour change.
    """

    VTUBER = "vtuber"
    PODCAST = "podcast"
    NEWS = "news"
    GAMING = "gaming"
    ASMR = "asmr"

    @classmethod
    def coerce(cls, value: Any) -> "ScoringProfile":
        """Best-effort string → enum coercion that never raises."""
        if isinstance(value, ScoringProfile):
            return value
        try:
            return cls(str(value).lower())
        except (ValueError, AttributeError):
            return cls.VTUBER


@dataclass(frozen=True)
class ProfileWeights:
    """Weight table for one Scoring Profile.

    The five LLM weights must sum to ≤ 1.0 (the remaining slack lets
    deterministic contributors push the total up to 10.0).  Determinist
    weights (``audio_norm_w`` / ``chat_norm_w``) are applied on top of
    the LLM-weighted total — see ``ClipScore.total_for`` for the math.
    """

    # LLM rubric weights (sum should be ~0.80 to leave room for detminst)
    retention_hook: float
    emotional_intensity: float
    completeness: float
    replayability: float
    shorts_friendly: float

    # Deterministic weight multipliers (each 0..0.10 typically)
    audio_norm_w: float
    chat_norm_w: float
    duration_fit_w: float
    # Bonus weight for audio-peak AND chat-spike co-occurrence inside the
    # same Moment range. The audit found this is the highest-precision
    # predictor of clip-worthiness; per-profile so ASMR / news can keep
    # the multiplier near zero. Defaulted so existing ProfileWeights
    # callers still construct cleanly. See May-28 audit "#6".
    coincidence_bonus_w: float = 0.0


# ─── Profile tables ──────────────────────────────────────────────────────────
#
# Design notes on each profile:
#
# - VTUBER (default): matches the legacy weights from models/clip.py so
#   nothing changes for current users. High retention_hook + emotional
#   intensity + audio peak weight reflects the loud-spike-driven nature
#   of VTuber livestream highlights.
# - PODCAST: rewards completeness + replayability (a self-contained
#   takeaway), penalises pure audio peaks (interview content rarely
#   spikes in dB), and skips chat weight (most podcasts have no chat).
# - NEWS: rewards completeness + retention_hook (the lede), suppresses
#   emotional_intensity (news cuts shouldn't be drama-driven), and
#   keeps chat at zero.
# - GAMING: between VTuber and podcast — emotion matters, audio peaks
#   matter, but completeness has weight too because gameplay clips need
#   setup→payoff structure to land.
# - ASMR: inverts the audio-peak heuristic. The relevant deterministic
#   feature for ASMR is *quietness consistency*, not peaks. We keep the
#   audio weight at near-zero so a clip is not punished for never going
#   loud, and lean entirely on LLM rubric.

PROFILES: dict[ScoringProfile, ProfileWeights] = {
    ScoringProfile.VTUBER: ProfileWeights(
        retention_hook=0.25,
        emotional_intensity=0.20,
        completeness=0.15,
        replayability=0.10,
        shorts_friendly=0.10,
        audio_norm_w=0.05,
        chat_norm_w=0.05,
        duration_fit_w=0.10,
        coincidence_bonus_w=0.10,    # audio peak + chat spike = jackpot
    ),
    ScoringProfile.PODCAST: ProfileWeights(
        retention_hook=0.20,
        emotional_intensity=0.10,
        completeness=0.25,
        replayability=0.20,
        shorts_friendly=0.05,
        audio_norm_w=0.02,
        chat_norm_w=0.0,
        duration_fit_w=0.18,
        coincidence_bonus_w=0.0,     # podcasts rarely have live chat
    ),
    ScoringProfile.NEWS: ProfileWeights(
        retention_hook=0.30,
        emotional_intensity=0.05,
        completeness=0.30,
        replayability=0.05,
        shorts_friendly=0.10,
        audio_norm_w=0.02,
        chat_norm_w=0.0,
        duration_fit_w=0.18,
        coincidence_bonus_w=0.0,
    ),
    ScoringProfile.GAMING: ProfileWeights(
        retention_hook=0.20,
        emotional_intensity=0.15,
        completeness=0.20,
        replayability=0.10,
        shorts_friendly=0.10,
        audio_norm_w=0.07,
        chat_norm_w=0.05,
        duration_fit_w=0.13,
        coincidence_bonus_w=0.08,    # crowd reaction matters in gaming clips
    ),
    ScoringProfile.ASMR: ProfileWeights(
        retention_hook=0.15,
        emotional_intensity=0.10,
        completeness=0.20,
        replayability=0.25,
        shorts_friendly=0.10,
        audio_norm_w=0.0,            # peaks are anti-signal in ASMR
        chat_norm_w=0.0,
        duration_fit_w=0.20,
        coincidence_bonus_w=0.0,     # ASMR clips are about consistency, not spikes
    ),
}


def weights_for(profile: ScoringProfile | str) -> ProfileWeights:
    """Return the weight table for the given profile name.

    Falls back to ``VTUBER`` if the name is unrecognised — defensive so
    bad config never raises in the hot path of scoring.
    """
    key = ScoringProfile.coerce(profile)
    return PROFILES.get(key, PROFILES[ScoringProfile.VTUBER])


def list_profile_names() -> list[str]:
    """Profile enum values in display order — used by the UI segmented control."""
    return [p.value for p in (
        ScoringProfile.VTUBER,
        ScoringProfile.PODCAST,
        ScoringProfile.NEWS,
        ScoringProfile.GAMING,
        ScoringProfile.ASMR,
    )]


__all__ = [
    "ScoringProfile",
    "ProfileWeights",
    "PROFILES",
    "weights_for",
    "list_profile_names",
]
