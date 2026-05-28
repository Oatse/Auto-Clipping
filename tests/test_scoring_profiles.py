"""Tests for processors.clip_finder.scoring_profiles."""

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
        assert ScoringProfile.coerce(ScoringProfile.PODCAST) == ScoringProfile.PODCAST

    def test_string_lookup(self):
        assert ScoringProfile.coerce("podcast") == ScoringProfile.PODCAST

    def test_uppercase_string_lowered(self):
        assert ScoringProfile.coerce("PODCAST") == ScoringProfile.PODCAST

    def test_unknown_falls_back_to_vtuber(self):
        assert ScoringProfile.coerce("anime") == ScoringProfile.VTUBER

    def test_none_falls_back_to_vtuber(self):
        assert ScoringProfile.coerce(None) == ScoringProfile.VTUBER


# ─── Profile table integrity ─────────────────────────────────────────────────

class TestProfileTableIntegrity:
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

    def test_vtuber_matches_legacy_weights(self):
        """VTuber profile must match the pre-ADR-0003 weights byte-for-byte."""
        w = PROFILES[ScoringProfile.VTUBER]
        assert w.retention_hook == 0.25
        assert w.emotional_intensity == 0.20
        assert w.completeness == 0.15
        assert w.replayability == 0.10
        assert w.shorts_friendly == 0.10
        assert w.audio_norm_w == 0.05
        assert w.chat_norm_w == 0.05
        assert w.duration_fit_w == 0.10


# ─── weights_for() resolver ──────────────────────────────────────────────────

class TestWeightsFor:
    def test_resolves_enum(self):
        assert weights_for(ScoringProfile.PODCAST) == PROFILES[ScoringProfile.PODCAST]

    def test_resolves_string(self):
        assert weights_for("news") == PROFILES[ScoringProfile.NEWS]

    def test_unknown_falls_back_to_vtuber(self):
        assert weights_for("k-pop") == PROFILES[ScoringProfile.VTUBER]


class TestProfileNames:
    def test_returns_canonical_order(self):
        names = list_profile_names()
        assert names[0] == "vtuber"
        assert "podcast" in names
        assert "asmr" in names
        assert len(names) == len(set(names))


# ─── ClipScore.total_for backward compatibility ──────────────────────────────

class TestClipScoreTotalForBackwardCompat:
    def test_default_property_matches_vtuber_profile(self):
        """Legacy ``total`` must equal ``total_for(VTUBER)`` byte-for-byte."""
        s = ClipScore(
            retention_hook=8.0,
            emotional_intensity=7.0,
            completeness=6.0,
            replayability=5.0,
            shorts_friendly=7.0,
            audio_peak_db=15.0,
            chat_spike_ratio=2.5,
            duration_fit=9.0,
        )
        assert s.total == s.total_for(ScoringProfile.VTUBER)

    def test_total_for_string_works(self):
        s = ClipScore(retention_hook=5.0, emotional_intensity=5.0)
        assert s.total_for("vtuber") == s.total_for(ScoringProfile.VTUBER)

    def test_total_for_unknown_falls_back_to_vtuber(self):
        s = ClipScore(retention_hook=5.0)
        assert s.total_for("nonexistent") == s.total_for(ScoringProfile.VTUBER)


# ─── Profile differentiation ─────────────────────────────────────────────────


class TestProfileDifferentiation:
    """Same candidate must score differently under different profiles."""

    def _spike_clip(self) -> ClipScore:
        """Loud audio peak + chat spike + low completeness — VTuber-ideal."""
        return ClipScore(
            retention_hook=8.0,
            emotional_intensity=9.0,
            completeness=3.0,
            replayability=4.0,
            shorts_friendly=8.0,
            audio_peak_db=25.0,    # +25 dB → very loud
            chat_spike_ratio=4.0,
            duration_fit=8.0,
        )

    def _explainer_clip(self) -> ClipScore:
        """High completeness, no audio peaks — podcast-ideal."""
        return ClipScore(
            retention_hook=7.0,
            emotional_intensity=4.0,
            completeness=9.0,
            replayability=8.0,
            shorts_friendly=6.0,
            audio_peak_db=0.0,
            chat_spike_ratio=0.0,
            duration_fit=8.0,
        )

    def test_spike_clip_scores_higher_under_vtuber_than_podcast(self):
        s = self._spike_clip()
        assert s.total_for(ScoringProfile.VTUBER) > s.total_for(ScoringProfile.PODCAST)

    def test_explainer_clip_scores_higher_under_podcast_than_vtuber(self):
        s = self._explainer_clip()
        assert s.total_for(ScoringProfile.PODCAST) > s.total_for(ScoringProfile.VTUBER)

    def test_asmr_does_not_reward_audio_peaks(self):
        """ASMR profile must keep the audio_norm_w near zero."""
        loud = ClipScore(
            retention_hook=5.0, emotional_intensity=5.0, completeness=5.0,
            replayability=5.0, shorts_friendly=5.0,
            audio_peak_db=30.0,    # would be a huge VTuber boost
        )
        quiet = ClipScore(
            retention_hook=5.0, emotional_intensity=5.0, completeness=5.0,
            replayability=5.0, shorts_friendly=5.0,
            audio_peak_db=0.0,
        )
        # Under VTuber, loud >> quiet
        assert loud.total_for(ScoringProfile.VTUBER) > quiet.total_for(ScoringProfile.VTUBER)
        # Under ASMR, the audio difference should not dominate
        delta_asmr = (
            loud.total_for(ScoringProfile.ASMR)
            - quiet.total_for(ScoringProfile.ASMR)
        )
        delta_vtuber = (
            loud.total_for(ScoringProfile.VTUBER)
            - quiet.total_for(ScoringProfile.VTUBER)
        )
        assert delta_asmr < delta_vtuber

    def test_news_suppresses_emotional_intensity(self):
        """News profile must rank emotional spikes below vtuber baseline."""
        emo = ClipScore(
            retention_hook=5.0, emotional_intensity=10.0, completeness=5.0,
            replayability=5.0, shorts_friendly=5.0,
        )
        calm = ClipScore(
            retention_hook=5.0, emotional_intensity=0.0, completeness=5.0,
            replayability=5.0, shorts_friendly=5.0,
        )
        delta_news = emo.total_for(ScoringProfile.NEWS) - calm.total_for(ScoringProfile.NEWS)
        delta_vtuber = emo.total_for(ScoringProfile.VTUBER) - calm.total_for(ScoringProfile.VTUBER)
        assert delta_news < delta_vtuber


# ─── Score never escapes [0, 10] ─────────────────────────────────────────────

class TestScoreClamping:
    def test_extreme_inputs_clamped_to_ten(self):
        s = ClipScore(
            retention_hook=10.0,
            emotional_intensity=10.0,
            completeness=10.0,
            replayability=10.0,
            shorts_friendly=10.0,
            audio_peak_db=999.0,
            chat_spike_ratio=999.0,
            duration_fit=10.0,
        )
        for profile in ScoringProfile:
            assert 0.0 <= s.total_for(profile) <= 10.0

    def test_zero_inputs_score_zero(self):
        s = ClipScore()
        for profile in ScoringProfile:
            assert s.total_for(profile) == 0.0


# ─── ADR-0005: score_profile travels with Clip ───────────────────────────────


class TestClipScoreProfileField:
    """ADR-0005: ``Clip.score_profile`` drives serialised ``score.total``."""

    def _spike_score(self) -> ClipScore:
        return ClipScore(
            retention_hook=8.0,
            emotional_intensity=9.0,
            completeness=3.0,
            replayability=4.0,
            shorts_friendly=8.0,
            audio_peak_db=25.0,
            chat_spike_ratio=4.0,
            duration_fit=8.0,
        )

    def test_to_dict_total_reflects_profile(self):
        from models.clip import Clip

        score = self._spike_score()
        clip_vt = Clip(start=0, end=20, title="x", score=score,
                       score_profile="vtuber")
        clip_pc = Clip(start=0, end=20, title="x", score=score,
                       score_profile="podcast")
        # Same raw ClipScore → different total in serialised form.
        assert clip_vt.to_dict()["score"]["total"] != clip_pc.to_dict()["score"]["total"]
        # And each total matches ``total_for(profile)`` exactly.
        assert clip_vt.to_dict()["score"]["total"] == score.total_for("vtuber")
        assert clip_pc.to_dict()["score"]["total"] == score.total_for("podcast")

    def test_default_score_profile_is_vtuber(self):
        from models.clip import Clip

        clip = Clip(start=0, end=20, title="x")
        assert clip.score_profile == "vtuber"
        # Default round-trip preserves legacy behaviour byte-for-byte.
        assert clip.to_dict()["score"]["total"] == clip.score.total

    def test_round_trip_preserves_profile(self):
        from models.clip import Clip

        clip = Clip(start=0, end=20, title="x", score=self._spike_score(),
                    score_profile="news")
        d = clip.to_dict()
        restored = Clip.from_dict(d)
        assert restored.score_profile == "news"
        assert restored.to_dict()["score"]["total"] == d["score"]["total"]

    def test_unknown_profile_falls_back_silently(self):
        from models.clip import Clip

        clip = Clip(start=0, end=20, title="x", score=self._spike_score(),
                    score_profile="this-profile-does-not-exist")
        # Should not raise — ``weights_for`` falls back to VTuber.
        total = clip.to_dict()["score"]["total"]
        assert total == self._spike_score().total_for("vtuber")
