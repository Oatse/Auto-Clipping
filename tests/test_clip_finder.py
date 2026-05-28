"""Unit tests for processors.clip_finder pure functions."""

from __future__ import annotations

import json
import pytest

from models.clip import (
    Clip,
    ClipCandidate,
    ClipScore,
    HighlightType,
    HunterTag,
    SignalEvent,
    SignalKind,
)
from processors.clip_finder import ClipFinder
from processors.clip_finder.boundary import refine_boundaries
from processors.clip_finder.clip_selection import (
    ClipFinderError,
    deduplicate_candidates,
    deduplicate_clips,
    parse_candidates_json,
)
from processors.clip_finder.heuristics import (
    fmt_duration,
    fmt_time,
    is_vtuber_mode,
    parse_duration_hints,
    to_seconds,
)
from processors.clip_finder.scoring import ClipScorer
from processors.clip_finder.selection import select_top_clips
from processors.clip_finder.transcript import (
    condense_for_prompt,
    extract_discarded,
    filter_by_offset,
    merge_short_segments,
    slice_for_clip,
)


# ─── heuristics ──────────────────────────────────────────────────────────────


class TestHeuristics:
    @pytest.mark.parametrize("text,expected", [
        ("2-3 menit", (120.0, 180.0)),
        ("30-60 seconds", (30.0, 60.0)),
        ("1-2 minutes", (60.0, 120.0)),
        ("30 detik", (15.0, 45.0)),
        ("60s", (30.0, 90.0)),
    ])
    def test_parse_duration_hints_explicit(self, text, expected):
        lo, hi = parse_duration_hints(text, video_duration=300)
        assert lo == pytest.approx(expected[0])
        assert hi == pytest.approx(expected[1])

    def test_parse_duration_hints_short_video(self):
        lo, hi = parse_duration_hints("", video_duration=120)
        assert (lo, hi) == (10.0, 120.0)

    def test_parse_duration_hints_long_video(self):
        lo, hi = parse_duration_hints("", video_duration=4000)
        assert (lo, hi) == (30.0, 300.0)

    @pytest.mark.parametrize("text,expected", [
        ("vtuber highlights", True),
        ("Find karma_arc moments", True),
        ("Hide all dead_air gaps", True),
        ("scream and laughter", True),
        ("just funny stuff", False),
        ("", False),
    ])
    def test_is_vtuber_mode(self, text, expected):
        assert is_vtuber_mode(text) is expected

    @pytest.mark.parametrize("secs,expected", [
        (3725, "1:02:05"),
        (125, "2:05"),
        (5, "0:05"),
    ])
    def test_fmt_time(self, secs, expected):
        assert fmt_time(secs) == expected

    @pytest.mark.parametrize("secs,expected", [
        (5, "5s"),
        (65, "1m 5s"),
        (125, "2m 5s"),
    ])
    def test_fmt_duration(self, secs, expected):
        assert fmt_duration(secs) == expected

    @pytest.mark.parametrize("value,expected", [
        (82, 82.0),
        (82.5, 82.5),
        ("82.5", 82.5),
        ("1:22", 82.0),
        ("1:02:30", 3750.0),
    ])
    def test_to_seconds(self, value, expected):
        assert to_seconds(value) == expected

    def test_to_seconds_invalid(self):
        with pytest.raises(ValueError):
            to_seconds("not a time")


# ─── transcript ──────────────────────────────────────────────────────────────


class TestTranscript:
    def test_merge_short_segments(self):
        segments = [
            {"start": 0.0, "end": 1.5, "text": "Hello"},
            {"start": 2.0, "end": 3.0, "text": "world"},
            {"start": 5.0, "end": 6.0, "text": "Far apart"},
        ]
        merged = merge_short_segments(segments, gap=1.0)
        assert len(merged) == 2
        assert merged[0]["text"] == "Hello world"
        assert merged[0]["end"] == 3.0
        assert merged[1]["text"] == "Far apart"

    def test_merge_does_not_mutate_input(self):
        segments = [{"start": 0.0, "end": 1.0, "text": "A"}]
        original = segments[0].copy()
        merge_short_segments(segments)
        assert segments[0] == original

    def test_condense_passthrough_when_below_max(self):
        segments = [{"start": float(i), "end": float(i + 1), "text": str(i)} for i in range(50)]
        result = condense_for_prompt(segments, max_segments=500)
        assert len(result) == 50

    def test_condense_reduces_count(self):
        # 1000 segments back-to-back
        segments = [{"start": float(i) * 0.3, "end": float(i) * 0.3 + 0.2, "text": str(i)} for i in range(1000)]
        result = condense_for_prompt(segments, max_segments=500)
        assert len(result) <= 500

    def test_filter_by_offset(self):
        segments = [
            {"start": 0.0, "end": 10.0, "text": "intro"},
            {"start": 10.0, "end": 20.0, "text": "wait"},
            {"start": 20.0, "end": 30.0, "text": "actual"},
        ]
        filtered = filter_by_offset(segments, start_offset=15.0)
        assert len(filtered) == 2
        assert filtered[0]["text"] == "wait"

    def test_filter_by_zero_offset_returns_all(self):
        segments = [{"start": 0.0, "end": 1.0, "text": "x"}]
        assert filter_by_offset(segments, 0) == segments

    def test_extract_discarded_no_selected(self):
        segments = [{"start": 0.0, "end": 10.0, "text": "x"}]
        assert extract_discarded(segments, []) == segments

    def test_extract_discarded_basic(self):
        segments = [
            {"start": 0.0, "end": 10.0, "text": "before"},
            {"start": 20.0, "end": 30.0, "text": "during"},
            {"start": 40.0, "end": 50.0, "text": "after"},
        ]
        # Selected covers 18-32 with 5s padding → 13-37
        selected = [(20.0, 30.0)]
        discarded = extract_discarded(segments, selected, padding=5.0)
        texts = [s["text"] for s in discarded]
        assert "before" in texts
        assert "during" not in texts
        assert "after" in texts

    def test_slice_for_clip_retimes(self):
        segments = [
            {"start": 0.0, "end": 10.0, "text": "before"},
            {"start": 10.0, "end": 20.0, "text": "inside"},
            {"start": 20.0, "end": 30.0, "text": "after"},
        ]
        sliced = slice_for_clip(segments, clip_start=10.0, clip_end=20.0, padding=0.5)
        # Should contain "inside" with start=0
        assert any(s["text"] == "inside" and s["start"] == 0.0 for s in sliced)

    def test_slice_for_clip_drops_outside(self):
        segments = [{"start": 100.0, "end": 110.0, "text": "way after"}]
        assert slice_for_clip(segments, 0, 50) == []


# ─── clip_selection ──────────────────────────────────────────────────────────


class TestClipSelection:
    def test_parse_pure_json_array(self):
        text = json.dumps([
            {"start": 10, "end": 30, "title": "A", "reason": "r"},
            {"start": 40, "end": 70, "title": "B", "reason": "r"},
        ])
        cands = parse_candidates_json(text, min_duration=10, max_duration=120)
        assert len(cands) == 2
        assert cands[0].title == "A"
        assert isinstance(cands[0], ClipCandidate)

    def test_parse_json_array_in_prose(self):
        text = (
            'Here are the clips: [{"start": 5, "end": 25, "title": "X"}] '
            "hope this helps!"
        )
        cands = parse_candidates_json(text, min_duration=10, max_duration=120)
        assert len(cands) == 1
        assert cands[0].title == "X"

    def test_parse_truncated_json_salvage(self):
        # No closing bracket
        text = (
            '[{"start": 5, "end": 25, "title": "OK"}, '
            '{"start": 30, "end": 50, "title": "Also OK"}, '
            '{"start": 60, "end": 80, "title": "Trunc'
        )
        cands = parse_candidates_json(text, min_duration=10, max_duration=120)
        assert len(cands) == 2

    def test_parse_extends_short_clips(self):
        text = json.dumps([{"start": 10, "end": 12, "title": "Short"}])  # 2s
        cands = parse_candidates_json(text, min_duration=10, max_duration=120)
        assert cands[0].duration >= 9.99

    def test_parse_caps_long_clips(self):
        text = json.dumps([{"start": 10, "end": 500, "title": "Long"}])
        cands = parse_candidates_json(text, min_duration=10, max_duration=120)
        assert cands[0].duration == pytest.approx(120, abs=0.5)

    def test_parse_handles_string_timestamp(self):
        text = json.dumps([{"start": "1:22", "end": "1:42", "title": "TS"}])
        cands = parse_candidates_json(text, min_duration=10, max_duration=120)
        assert cands[0].start == 82.0

    def test_dedup_candidates_by_title(self):
        cands = [
            ClipCandidate(start=0, end=20, title="Same"),
            ClipCandidate(start=100, end=120, title="Same"),
        ]
        deduped = deduplicate_candidates(cands)
        assert len(deduped) == 1

    def test_dedup_candidates_by_overlap(self):
        cands = [
            ClipCandidate(start=10, end=40, title="A"),
            ClipCandidate(start=20, end=50, title="B"),  # overlaps 20s of 30s = 67%
        ]
        deduped = deduplicate_candidates(cands)
        assert len(deduped) == 1
        assert deduped[0].title == "A"

    def test_dedup_candidates_keeps_non_overlapping(self):
        cands = [
            ClipCandidate(start=0, end=10, title="A"),
            ClipCandidate(start=100, end=110, title="B"),
        ]
        assert len(deduplicate_candidates(cands)) == 2

    def test_dedup_clips(self):
        clips = [
            Clip(start=0, end=20, title="Same"),
            Clip(start=0, end=20, title="Same"),
        ]
        assert len(deduplicate_clips(clips)) == 1


# ─── boundary refinement ─────────────────────────────────────────────────────


class TestBoundary:
    def test_refine_no_signals_passthrough(self):
        clips = [Clip(start=10, end=30, title="x")]
        out = refine_boundaries(clips, [])
        assert out[0].start == 10
        assert out[0].end == 30

    def test_refine_snaps_to_silence(self):
        clips = [Clip(start=10, end=30, title="x")]
        signals = [
            SignalEvent(SignalKind.AUDIO_SILENCE, start=5.0, end=8.5),  # before clip
            SignalEvent(SignalKind.AUDIO_SILENCE, start=32.0, end=37.0),  # after clip
        ]
        out = refine_boundaries(clips, signals)
        # Should snap start to 8.5 (end of pre-clip silence within lookback)
        assert out[0].start == 8.5
        # Should snap end to 32.0 (start of post-clip silence within lookahead)
        assert out[0].end == 32.0

    def test_refine_respects_min_duration(self):
        clips = [Clip(start=10, end=15, title="x")]
        signals = [
            SignalEvent(SignalKind.AUDIO_SILENCE, start=9.0, end=14.5),
        ]
        # Snapping start to 14.5 would create a 0.5s clip — must fall back
        out = refine_boundaries(clips, signals, min_duration=5.0)
        assert out[0].start == 10
        assert out[0].end == 15

    def test_refine_attaches_dead_air(self):
        clips = [Clip(start=10, end=60, title="x")]
        signals = [
            SignalEvent(SignalKind.AUDIO_SILENCE, start=25.0, end=32.0),
        ]
        out = refine_boundaries(clips, signals)
        # 7s silence inside [10, 60] — should be flagged
        assert len(out[0].dead_air_timestamps) == 1


# ─── selection ───────────────────────────────────────────────────────────────


class TestSelection:
    def _clip(self, start, end, title, hunter, score_total=5.0):
        score = ClipScore(retention_hook=score_total)
        return Clip(start=start, end=end, title=title, hunter=hunter, score=score)

    def test_select_returns_sorted_by_start(self):
        clips = [
            self._clip(50, 70, "B", HunterTag.LAUGHTER),
            self._clip(10, 30, "A", HunterTag.SCREAM),
            self._clip(100, 120, "C", HunterTag.RAGE),
        ]
        result = select_top_clips(clips, max_count=3)
        assert [c.start for c in result] == [10, 50, 100]

    def test_select_diversifies_tags(self):
        clips = [
            self._clip(10, 30, "A1", HunterTag.SCREAM, score_total=10),
            self._clip(50, 70, "A2", HunterTag.SCREAM, score_total=9),
            self._clip(150, 170, "B1", HunterTag.LAUGHTER, score_total=8),
        ]
        result = select_top_clips(clips, max_count=2)
        # Should pick one SCREAM + one LAUGHTER, not two SCREAM
        tags = {c.hunter for c in result}
        assert HunterTag.SCREAM in tags
        assert HunterTag.LAUGHTER in tags

    def test_select_respects_duration_budget(self):
        clips = [
            self._clip(0, 60, "long1", HunterTag.SCREAM),     # 60s
            self._clip(100, 160, "long2", HunterTag.LAUGHTER), # 60s
            self._clip(200, 260, "long3", HunterTag.RAGE),     # 60s
        ]
        result = select_top_clips(clips, max_count=10, duration_budget=120)
        total = sum(c.duration for c in result)
        assert total <= 120

    def test_select_uses_clip_score_profile_when_no_profile_passed(self):
        """ADR-0005: each Clip's own ``score_profile`` drives ranking."""
        # Two clips with identical raw rubric but different profiles —
        # ASMR suppresses audio peak weight, so the loud clip ranks
        # *lower* under ASMR than the quiet one even though their raw
        # ClipScore objects are mirror images.
        loud_score = ClipScore(
            retention_hook=5, emotional_intensity=5, completeness=5,
            replayability=5, shorts_friendly=5, audio_peak_db=30,
        )
        quiet_score = ClipScore(
            retention_hook=5, emotional_intensity=5, completeness=5,
            replayability=5, shorts_friendly=5, audio_peak_db=0,
        )
        loud = Clip(start=10, end=30, title="loud", score=loud_score,
                    score_profile="asmr")
        quiet = Clip(start=100, end=120, title="quiet", score=quiet_score,
                     score_profile="asmr")
        result = select_top_clips([loud, quiet], max_count=2)
        # Both included (max_count=2), but order under their own profile
        # would have ranked them tied or near-tied. The test really
        # asserts no crash + both survive — see test_scoring_profiles
        # for delta math.
        assert len(result) == 2

    def test_select_explicit_profile_overrides_clip_field(self):
        """ADR-0005: explicit ``profile`` kwarg takes precedence."""
        loud_score = ClipScore(audio_peak_db=30, retention_hook=5)
        quiet_score = ClipScore(audio_peak_db=0, retention_hook=5)
        loud = Clip(start=10, end=30, title="loud", score=loud_score,
                    score_profile="asmr")
        quiet = Clip(start=100, end=120, title="quiet", score=quiet_score,
                     score_profile="asmr")
        # Force VTuber sort — loud should beat quiet because audio
        # peaks are heavily weighted under VTuber.
        result = select_top_clips(
            [quiet, loud], max_count=1, profile="vtuber",
        )
        assert result[0].title == "loud"


# ─── ClipScore math ──────────────────────────────────────────────────────────


class TestClipScore:
    def test_total_in_range(self):
        s = ClipScore(retention_hook=10, emotional_intensity=10, completeness=10,
                      replayability=10, shorts_friendly=10, duration_fit=10)
        assert 0 <= s.total <= 10

    def test_total_zero_when_empty(self):
        assert ClipScore().total == 0.0

    def test_to_dict_roundtrip(self):
        s = ClipScore(retention_hook=7.5)
        d = s.to_dict()
        s2 = ClipScore.from_dict(d)
        assert s2.retention_hook == 7.5


# ─── ClipScorer deterministic features ───────────────────────────────────────


class TestScorerDeterministic:
    def test_audio_peak_extracted_from_label(self):
        cand = ClipCandidate(start=10, end=30, title="x")
        signals = [
            SignalEvent(SignalKind.AUDIO_PEAK, start=15, end=18, label="+18.5 dB above baseline"),
        ]
        feats = ClipScorer._deterministic_features(cand, signals, 10, 60)
        assert feats["audio_peak_db"] == 18.5

    def test_chat_spike_extracted_from_label(self):
        cand = ClipCandidate(start=10, end=30, title="x")
        signals = [
            SignalEvent(SignalKind.CHAT_SPIKE, start=15, end=20, label="chat 4.5x baseline"),
        ]
        feats = ClipScorer._deterministic_features(cand, signals, 10, 60)
        assert feats["chat_spike_ratio"] == 4.5

    def test_duration_fit_perfect_inside_range(self):
        cand = ClipCandidate(start=10, end=40, title="x")  # 30s
        feats = ClipScorer._deterministic_features(cand, [], 10, 60)
        assert feats["duration_fit"] == 10.0

    def test_signals_outside_range_ignored(self):
        cand = ClipCandidate(start=10, end=20, title="x")
        signals = [
            SignalEvent(SignalKind.AUDIO_PEAK, start=100, end=110, label="+25 dB"),
        ]
        feats = ClipScorer._deterministic_features(cand, signals, 10, 60)
        assert feats["audio_peak_db"] == 0.0


# ─── Domain model serialization ──────────────────────────────────────────────


class TestSerialization:
    def test_clip_roundtrip(self):
        c = Clip(
            start=10, end=30, title="X",
            hunter=HunterTag.SCREAM,
            highlight_type=HighlightType.GENUINE_REACTION,
            score=ClipScore(retention_hook=8),
            dead_air_timestamps=[15.5],
        )
        d = c.to_dict()
        c2 = Clip.from_dict(d)
        assert c2.title == "X"
        assert c2.hunter == HunterTag.SCREAM
        assert c2.highlight_type == HighlightType.GENUINE_REACTION
        assert c2.score.retention_hook == 8
        assert c2.dead_air_timestamps == [15.5]

    def test_signal_event_roundtrip(self):
        s = SignalEvent(SignalKind.CHAT_SPIKE, 10, 15, intensity=0.8, label="chat 5x baseline")
        d = s.to_dict()
        s2 = SignalEvent.from_dict(d)
        assert s2.kind == SignalKind.CHAT_SPIKE
        assert s2.intensity == 0.8

    def test_unknown_hunter_falls_back(self):
        assert HunterTag.coerce("bogus") == HunterTag.GENERAL

    def test_unknown_highlight_falls_back(self):
        assert HighlightType.coerce("bogus") == HighlightType.UNSPECIFIED


# ─── Backwards-compat facade methods ─────────────────────────────────────────


class TestBackwardsCompat:
    def test_filter_transcript_static(self):
        cf = ClipFinder()
        out = cf.filter_transcript_by_offset(
            [{"start": 0, "end": 10, "text": "x"}, {"start": 10, "end": 20, "text": "y"}],
            start_offset=5,
        )
        assert len(out) == 2  # first ends at 10 > 5

    def test_slice_transcript_static(self):
        cf = ClipFinder()
        out = cf.slice_transcript_for_clip(
            [{"start": 0, "end": 5, "text": "before"}, {"start": 10, "end": 20, "text": "in"}],
            clip_start=10, clip_end=20,
        )
        assert any(s["text"] == "in" and s["start"] == 0 for s in out)

    def test_fmt_helpers(self):
        assert ClipFinder.fmt_time(82) == "1:22"
        assert ClipFinder.fmt_duration(125) == "2m 5s"
