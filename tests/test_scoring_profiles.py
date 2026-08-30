"""Tests for processors.clip_finder.scoring_profiles (VTuber-only).

VTuber-refocus Step 4 collapsed the five content niches to a single
VTuber profile. These tests cover the surviving surface: legacy-value
coercion to VTuber, the single weight table's integrity and editorial
priorities (including the new interaction_dynamic / en_translatability /
format_fit dimensions), and score clamping.
"""

from __future__ import annotations

import pytest

from models.clip import ClipScore
from processors.clip_finder.scoring_profiles import (
    PROFILES,
    ProfileWeights,
    ScoringProfile,
    list_profile_names,
    weights_for,
)


# ─── ScoringProfile enum coercion ────────────────────────────────────────────

class TestProfileCoercion:
    def test_enum_passthrough(self):
        assert ScoringProfile.coerce(ScoringProfile.VTUBER) == ScoringProfile.VTUBER

    def test_string_lookup(self):
        assert ScoringProfile.coerce("vtuber") == ScoringProfile.VTUBER

    def test_uppercase_string_lowered(self):
        assert ScoringProfile.coerce("VTUBER") == ScoringProfile.VTUBER

    def test_legacy_niche_collapses_to_vtuber(self):
        # Old persisted jobs may still carry a retired niche name.
        for legacy in ("podcast", "news", "gaming", "asmr"):
            assert ScoringProfile.coerce(legacy) == ScoringProfile.VTUBER

    def test_unknown_falls_back_to_vtuber(self):
        assert ScoringProfile.coerce("anime") == ScoringProfile.VTUBER

    def test_none_falls_back_to_vtuber(self):
        assert ScoringProfile.coerce(None) == ScoringProfile.VTUBER


# ─── Profile table integrity ─────────────────────────────────────────────────

class TestProfileTableIntegrity:
    def test_only_vtuber_profile_exists(self):
        assert set(PROFILES.keys()) == {ScoringProfile.VTUBER}
        assert list(ScoringProfile) == [ScoringProfile.VTUBER]

    def test_every_profile_has_weights(self):
        for profile in ScoringProfile:
            assert profile in PROFILES, f"Missing weights for {profile.value}"

    def test_llm_weights_sum_within_budget(self):
        """LLM weight sum must leave room for deterministic contributors."""
        for profile, w in PROFILES.items():
            llm_sum = (
                w.retention_hook
                + w.emotional_intensity
                + w.completeness
                + w.replayability
                + w.shorts_friendly
                + w.quotability
                + w.character_moment
                + w.novelty
                + w.interaction_dynamic
                + w.en_translatability
            )
            assert 0.5 <= llm_sum <= 1.0, (
                f"{profile.value} llm weight sum {llm_sum} outside [0.5, 1.0]"
            )

    def test_no_negative_weights(self):
        for profile, w in PROFILES.items():
            for field, value in vars(w).items():
                assert value >= 0.0, (
                    f"{profile.value}.{field} = {value} (negative weights forbidden)"
                )

    def test_vtuber_prioritises_vtuber_native_dimensions(self):
        """Quotability + character together must outweigh the generic hook."""
        w = PROFILES[ScoringProfile.VTUBER]
        assert w.quotability + w.character_moment > (
            w.retention_hook + w.emotional_intensity
        )
        # Chat nomination beats raw loudness.
        assert w.clip_intent_w > w.audio_norm_w
        # Collab interaction is a first-class, heavily-weighted dimension.
        assert w.interaction_dynamic >= 0.08


# ─── weights_for() resolver ──────────────────────────────────────────────────

class TestWeightsFor:
    def test_resolves_enum(self):
        assert weights_for(ScoringProfile.VTUBER) == PROFILES[ScoringProfile.VTUBER]

    def test_resolves_string(self):
        assert weights_for("vtuber") == PROFILES[ScoringProfile.VTUBER]

    def test_legacy_and_unknown_fall_back_to_vtuber(self):
        for name in ("podcast", "news", "k-pop"):
            assert weights_for(name) == PROFILES[ScoringProfile.VTUBER]


class TestProfileNames:
    def test_returns_single_vtuber(self):
        names = list_profile_names()
        assert names == ["vtuber"]


# ─── ClipScore.total_for backward compatibility ──────────────────────────────

class TestClipScoreTotalForBackwardCompat:
    def test_default_property_matches_vtuber_profile(self):
        s = ClipScore(
            retention_hook=8.0, emotional_intensity=7.0, completeness=6.0,
            replayability=5.0, shorts_friendly=7.0,
            audio_peak_db=15.0, chat_spike_ratio=2.5, duration_fit=9.0,
        )
        assert s.total == s.total_for(ScoringProfile.VTUBER)

    def test_total_for_string_works(self):
        s = ClipScore(retention_hook=5.0, emotional_intensity=5.0)
        assert s.total_for("vtuber") == s.total_for(ScoringProfile.VTUBER)

    def test_legacy_profile_string_collapses_to_vtuber(self):
        s = ClipScore(retention_hook=5.0, quotability=8.0)
        assert s.total_for("podcast") == s.total_for(ScoringProfile.VTUBER)


# ─── VTuber editorial policy ──────────────────────────────────────────────────

class TestVtuberPolicy:
    """The single profile must encode the research-backed priorities."""

    def _spike_clip(self) -> ClipScore:
        return ClipScore(
            retention_hook=8.0, emotional_intensity=9.0, completeness=3.0,
            replayability=4.0, shorts_friendly=8.0,
            quotability=9.0, character_moment=8.0, novelty=7.0,
            audio_peak_db=25.0, chat_spike_ratio=4.0, duration_fit=8.0,
            clip_intent_score=8.0,
        )

    def _loud_but_empty_clip(self) -> ClipScore:
        return ClipScore(
            retention_hook=8.0, emotional_intensity=9.0, completeness=3.0,
            replayability=4.0, shorts_friendly=8.0,
            quotability=1.0, character_moment=1.0, novelty=2.0,
            audio_peak_db=25.0, chat_spike_ratio=4.0, duration_fit=8.0,
        )

    def test_loud_but_unquotable_loses_to_quotable(self):
        quotable = self._spike_clip().total_for(ScoringProfile.VTUBER)
        loud = self._loud_but_empty_clip().total_for(ScoringProfile.VTUBER)
        assert quotable > loud
        assert quotable - loud >= 1.5

    def test_interaction_dynamic_lifts_a_collab_clip(self):
        base = ClipScore(retention_hook=5.0, emotional_intensity=5.0)
        collab = ClipScore(
            retention_hook=5.0, emotional_intensity=5.0, interaction_dynamic=10.0
        )
        assert collab.total_for("vtuber") > base.total_for("vtuber")

    def test_en_translatability_lifts_a_translatable_clip(self):
        dies_in_tl = ClipScore(quotability=8.0, en_translatability=1.0)
        lands_in_en = ClipScore(quotability=8.0, en_translatability=10.0)
        assert lands_in_en.total_for("vtuber") > dies_in_tl.total_for("vtuber")

    def test_format_fit_rewards_channel_tier(self):
        off_tier = ClipScore(retention_hook=5.0, format_fit=0.0)
        on_tier = ClipScore(retention_hook=5.0, format_fit=10.0)
        assert on_tier.total_for("vtuber") > off_tier.total_for("vtuber")


# ─── Score never escapes [0, 10] ─────────────────────────────────────────────

class TestScoreClamping:
    def test_extreme_inputs_clamped_to_ten(self):
        s = ClipScore(
            retention_hook=10.0, emotional_intensity=10.0, completeness=10.0,
            replayability=10.0, shorts_friendly=10.0,
            quotability=10.0, character_moment=10.0, novelty=10.0,
            interaction_dynamic=10.0, en_translatability=10.0,
            audio_peak_db=999.0, chat_spike_ratio=999.0, duration_fit=10.0,
            coincidence_bonus=10.0, clip_intent_score=10.0, format_fit=10.0,
        )
        for profile in ScoringProfile:
            assert 0.0 <= s.total_for(profile) <= 10.0
        # Raw pre-clamp total (Step 0 tie-breaker) may exceed 10.
        assert s.total_for(ScoringProfile.VTUBER, clamp=False) > 10.0

    def test_zero_inputs_score_zero(self):
        s = ClipScore()
        for profile in ScoringProfile:
            assert s.total_for(profile) == 0.0


# ─── score_profile travels with Clip (now always VTuber) ──────────────────────

class TestClipScoreProfileField:
    def _spike_score(self) -> ClipScore:
        return ClipScore(
            retention_hook=8.0, emotional_intensity=9.0, completeness=3.0,
            replayability=4.0, shorts_friendly=8.0,
            audio_peak_db=25.0, chat_spike_ratio=4.0, duration_fit=8.0,
        )

    def test_default_score_profile_is_vtuber(self):
        from models.clip import Clip

        clip = Clip(start=0, end=20, title="x")
        assert clip.score_profile == "vtuber"
        assert clip.to_dict()["score"]["total"] == clip.score.total

    def test_legacy_profile_serialises_as_vtuber_total(self):
        from models.clip import Clip

        # A job persisted under a retired niche still deserialises and its
        # total is computed under VTuber (the only surviving table).
        clip = Clip(start=0, end=20, title="x", score=self._spike_score(),
                    score_profile="podcast")
        total = clip.to_dict()["score"]["total"]
        assert total == self._spike_score().total_for("vtuber")

    def test_round_trip_preserves_dict_total(self):
        from models.clip import Clip

        clip = Clip(start=0, end=20, title="x", score=self._spike_score(),
                    score_profile="vtuber")
        d = clip.to_dict()
        restored = Clip.from_dict(d)
        assert restored.to_dict()["score"]["total"] == d["score"]["total"]

    def test_unknown_profile_falls_back_silently(self):
        from models.clip import Clip

        clip = Clip(start=0, end=20, title="x", score=self._spike_score(),
                    score_profile="this-profile-does-not-exist")
        total = clip.to_dict()["score"]["total"]
        assert total == self._spike_score().total_for("vtuber")
