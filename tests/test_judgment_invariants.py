"""End-to-end judgment invariants for the VTuber-only clip finder.

These lock in the editorial guarantees the VTuber-refocus introduced,
exercised through the *real* scoring/selection/boundary code (not just
the weight table): the properties a regression must never silently break.

The real precision@k eval over a labelled VOD dataset (plan Step 10) is
intentionally not here — it needs real data the repo does not ship. This
module is the always-runnable invariant layer beneath that.
"""

from __future__ import annotations

from models.clip import Clip, ClipScore, SignalEvent, SignalKind
from processors.clip_finder import boundary, selection
from processors.clip_finder.scoring import ClipScorer, _format_fit


VT = "vtuber"


def _score(**kw) -> ClipScore:
    return ClipScore(**kw)


# ─── Chat is the primary driver ──────────────────────────────────────────────

class TestChatDrivesJudgment:
    def test_clip_intent_beats_pure_loudness(self):
        """A moment chat asked to clip must outrank an equally-rated moment
        that is merely loud."""
        requested = _score(
            retention_hook=6, emotional_intensity=6, quotability=6,
            clip_intent_score=10.0,          # chat typed "clip it"
        )
        just_loud = _score(
            retention_hook=6, emotional_intensity=6, quotability=6,
            audio_peak_db=30.0,              # very loud, nobody asked
        )
        assert requested.total_for(VT) > just_loud.total_for(VT)

    def test_coincidence_bonus_rewards_audio_and_chat_overlap(self):
        base = _score(retention_hook=6, emotional_intensity=6)
        jackpot = _score(retention_hook=6, emotional_intensity=6,
                         coincidence_bonus=10.0)
        assert jackpot.total_for(VT) > base.total_for(VT)


# ─── Personality / collab / translatability over generic ─────────────────────

class TestVtuberEditorialPriorities:
    def test_loud_but_empty_loses_to_quotable_in_character(self):
        quotable = _score(
            retention_hook=8, emotional_intensity=9, audio_peak_db=25,
            chat_spike_ratio=4, quotability=9, character_moment=8, novelty=7,
            clip_intent_score=8,
        )
        loud_empty = _score(
            retention_hook=8, emotional_intensity=9, audio_peak_db=25,
            chat_spike_ratio=4, quotability=1, character_moment=1, novelty=2,
        )
        assert quotable.total_for(VT) - loud_empty.total_for(VT) >= 1.5

    def test_collab_interaction_lifts_ranking(self):
        solo = _score(retention_hook=6, emotional_intensity=6)
        collab = _score(retention_hook=6, emotional_intensity=6,
                        interaction_dynamic=10.0)
        assert collab.total_for(VT) > solo.total_for(VT)

    def test_untranslatable_pun_ranks_below_translatable(self):
        dies = _score(quotability=9, en_translatability=1.0)
        lands = _score(quotability=9, en_translatability=10.0)
        assert lands.total_for(VT) > dies.total_for(VT)


# ─── format_fit reflects the channel's tiers ─────────────────────────────────

class TestFormatFit:
    def test_long_form_primary_tier_scores_max(self):
        assert _format_fit(300.0) == 10.0     # 5 min, primary

    def test_shorts_booster_tier_scores_high_but_below_primary(self):
        assert 0.0 < _format_fit(30.0) < _format_fit(300.0)

    def test_awkward_middle_scores_lower_than_both_tiers(self):
        mid = _format_fit(120.0)
        assert mid < _format_fit(30.0)
        assert mid < _format_fit(300.0)

    def test_overlong_decays(self):
        assert _format_fit(1800.0) < _format_fit(600.0)


# ─── Punchline survives boundary refinement (Step 0 regression) ──────────────

class TestPunchlineSurvivesRefine:
    def test_punchline_reanchored_after_start_snap(self):
        clip = Clip(start=10.0, end=40.0, title="t", score=ClipScore(),
                    score_profile=VT)
        clip.punchline_offset = 20.0                      # absolute t=30
        silences = [SignalEvent(kind=SignalKind.AUDIO_SILENCE,
                                start=4.0, end=8.0, intensity=1.0)]
        out = boundary.refine_boundaries([clip], silences, min_duration=3.0,
                                         transcript=None)
        r = out[0]
        assert abs(r.start - 8.0) < 0.01
        assert r.punchline_offset is not None
        assert abs(r.punchline_offset - 22.0) < 0.01     # 30 - 8
        assert r.score_profile == VT


# ─── Top-clip ranking survives the 10.0 clamp (Step 0 tie-break) ─────────────

class TestTopClipTieBreak:
    def test_higher_raw_wins_when_both_clamp_to_ten(self):
        strong = _score(
            retention_hook=10, emotional_intensity=10, completeness=10,
            replayability=10, shorts_friendly=10, quotability=10,
            character_moment=10, novelty=10, interaction_dynamic=10,
            en_translatability=10, coincidence_bonus=10, clip_intent_score=10,
            duration_fit=10, format_fit=10,
        )
        weaker = _score(
            retention_hook=9, emotional_intensity=9, completeness=9,
            replayability=9, shorts_friendly=9, quotability=9,
            character_moment=9, novelty=9, interaction_dynamic=9,
            en_translatability=9, coincidence_bonus=10, clip_intent_score=10,
            duration_fit=10, format_fit=10,
        )
        assert strong.total_for(VT) == 10.0 and weaker.total_for(VT) == 10.0
        a = Clip(start=0, end=20, title="A", score=strong, score_profile=VT)
        b = Clip(start=100, end=120, title="B", score=weaker, score_profile=VT)
        top1 = selection.select_top_clips([b, a], max_count=1, profile=VT)
        assert top1[0].title == "A"


# ─── Scorer deterministic features include format_fit ────────────────────────

class TestScorerDeterministic:
    def test_format_fit_populated_from_duration(self):
        from models.clip import ClipCandidate

        cand = ClipCandidate(start=0.0, end=300.0, title="x")   # 5 min
        feats = ClipScorer._deterministic_features(cand, [], 15.0, 600.0)
        assert feats["format_fit"] == 10.0
