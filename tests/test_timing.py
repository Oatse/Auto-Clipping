"""Tests for processors.timing — TimingPolicy + Sanitizer."""

from __future__ import annotations

import pytest

from models.transcript import TranscriptSegment, WordTimestamp
from processors.timing import Sanitizer, TimingPolicy
from processors.timing.sanitizer import estimate_max_word_duration


def _seg(start, end, speaker, words):
    return TranscriptSegment(
        start=start, end=end, speaker=speaker,
        text=" ".join(w[0] for w in words),
        words=[WordTimestamp(word=w[0], start=w[1], end=w[2]) for w in words],
    )


# ─── Word duration estimate ──────────────────────────────────────────────

class TestEstimateMaxWordDuration:
    def test_normal_word_clamped_to_max(self):
        policy = TimingPolicy()
        # "beautiful" = 9 chars * 0.09 + 0.15 = 0.96, below max 1.5
        assert estimate_max_word_duration("beautiful", policy) == pytest.approx(0.96, abs=0.01)

    def test_short_word_floored(self):
        policy = TimingPolicy()
        # "i" = 1 char * 0.09 + 0.15 = 0.24, floored to 0.30
        assert estimate_max_word_duration("i", policy) == pytest.approx(0.30, abs=0.01)

    def test_very_long_normal_word_capped(self):
        policy = TimingPolicy()
        # 30 unique chars × 0.09 + 0.15 = 2.85, capped to max 1.5
        assert estimate_max_word_duration("abcdefghijklmnopqrstuvwxyzabcd", policy) == 1.5

    def test_elongated_word_bypasses_cap(self):
        policy = TimingPolicy()
        # "noooooo" = n + 6 o's. longest run = 6 o's. base = 7*0.09+0.15 = 0.78
        # elongation budget = 6 * 0.35 = 2.10 → 0.78 + 2.10 = 2.88
        result = estimate_max_word_duration("noooooo", policy)
        assert result > 1.5, f"elongated word should bypass max cap: got {result}"
        assert result == pytest.approx(2.88, abs=0.01)

    def test_empty_word_returns_min(self):
        policy = TimingPolicy()
        assert estimate_max_word_duration("", policy) == policy.duration_min

    def test_elongation_run_threshold_respected(self):
        # 2 repeats does not count as elongated
        policy = TimingPolicy(elongation_run_threshold=3)
        # "moo" longest run=2 → normal path
        assert estimate_max_word_duration("moo", policy) <= 1.5

    def test_custom_policy_overrides_constants(self):
        policy = TimingPolicy(duration_max=0.5)
        # "beautiful" base 0.96, but max=0.5 → clamp
        assert estimate_max_word_duration("beautiful", policy) == 0.5


# ─── Speaker-aware sanitization ──────────────────────────────────────────

class TestSpeakerAwareSanitize:
    def test_cross_speaker_overlap_preserved(self):
        """Speaker A talks until 2.0s, Speaker B starts at 1.5s (interrupt)."""
        segs = [
            _seg(0.0, 2.0, "SPEAKER_00", [("hello", 0.0, 1.0), ("world", 1.0, 2.0)]),
            _seg(1.5, 3.0, "SPEAKER_01", [("wait", 1.5, 2.2), ("what", 2.2, 3.0)]),
        ]
        Sanitizer().sanitize(segs)
        # Speaker A's end MUST stay near 2.0 (not trimmed to 1.5)
        assert segs[0].end >= 1.9, f"cross-speaker trim applied: end={segs[0].end}"
        assert segs[0].words[-1].end >= 1.9

    def test_same_speaker_overlap_trimmed(self):
        segs = [
            _seg(0.0, 2.0, "SPEAKER_00", [("hello", 0.0, 2.0)]),
            _seg(1.5, 3.0, "SPEAKER_00", [("world", 1.5, 3.0)]),
        ]
        Sanitizer().sanitize(segs)
        # Pass 2 (same-speaker word overlap) trims the first word's end to
        # the next word's start (1.5), then Pass 3 snaps seg.end to that.
        # The result is non-overlapping but touching segments.
        assert segs[0].end <= segs[1].start, (
            f"same-speaker overlap not trimmed: "
            f"seg0.end={segs[0].end}, seg1.start={segs[1].start}"
        )
        assert segs[0].words[-1].end <= segs[0].end

    def test_same_speaker_word_overlap_trimmed_across_segments(self):
        segs = [
            _seg(0.0, 1.5, "SPEAKER_00", [("hello", 0.0, 1.5)]),
            _seg(1.0, 2.5, "SPEAKER_00", [("world", 1.0, 2.5)]),
        ]
        Sanitizer().sanitize(segs)
        # 'hello' end was 1.5, 'world' starts at 1.0
        # → 'hello'.end should be trimmed to <= 1.0
        assert segs[0].words[0].end <= segs[1].words[0].start


class TestWordDurationCap:
    def test_broken_word_capped(self):
        # Word "hi" with duration 10s — clearly broken
        segs = [_seg(0.0, 10.0, "SPEAKER_00", [("hi", 0.0, 10.0)])]
        Sanitizer(TimingPolicy(silence_cap=1.0)).sanitize(segs)
        assert segs[0].words[0].end < 5.0, (
            f"broken duration not capped: end={segs[0].words[0].end}"
        )

    def test_normal_word_within_cap_unchanged(self):
        # 1-second "hello" is well within the cap (estimate ~0.6 + 2.0 slack = 2.6)
        segs = [_seg(0.0, 1.0, "SPEAKER_00", [("hello", 0.0, 1.0)])]
        Sanitizer().sanitize(segs)
        assert segs[0].words[0].end == 1.0

    def test_zero_duration_word_repaired(self):
        segs = [_seg(0.0, 0.0, "SPEAKER_00", [("hello", 0.0, 0.0)])]
        Sanitizer().sanitize(segs)
        assert segs[0].words[0].end > segs[0].words[0].start


class TestSpeechRateAdaptation:
    def test_fast_speech_loosens_cap(self):
        """All words have actual duration > estimated → cap loosens."""
        # 5 words each 1.5x their estimated duration
        # base "ab" estimate = 0.33s, give them 0.5s actual (1.51× factor)
        segs = [_seg(0.0, 5.0, "SPEAKER_00", [
            ("ab", i * 1.0, i * 1.0 + 0.5) for i in range(5)
        ])]
        # Without speech-rate adaptation, the words would be capped to 0.33s.
        # With adaptation, factor ~1.51 lets 0.5s pass through.
        Sanitizer(TimingPolicy(silence_cap=0.0)).sanitize(segs)
        # Each word should KEEP its 0.5s duration
        for w in segs[0].words:
            assert w.end - w.start == pytest.approx(0.5, abs=0.05), (
                f"speech-rate did not loosen cap: {w.end - w.start}"
            )

    def test_normal_speech_does_not_tighten_below_baseline(self):
        # All words exactly at baseline → factor stays at 1.0
        segs = [_seg(0.0, 5.0, "SPEAKER_00", [
            ("hello", i * 1.0, i * 1.0 + 0.6) for i in range(5)  # exact estimate
        ])]
        Sanitizer().sanitize(segs)
        for w in segs[0].words:
            assert w.end - w.start == pytest.approx(0.6, abs=0.05)


# ─── Segment-level passes ────────────────────────────────────────────────

class TestSegmentPasses:
    def test_segments_sorted_chronologically(self):
        segs = [
            _seg(2.0, 3.0, "SPEAKER_00", [("late", 2.0, 3.0)]),
            _seg(0.0, 1.0, "SPEAKER_00", [("early", 0.0, 1.0)]),
        ]
        Sanitizer().sanitize(segs)
        assert segs[0].start < segs[1].start

    def test_segment_boundary_recalculated_from_words(self):
        # Initial seg.end is wrong (5.0), but words say 1.0
        segs = [_seg(0.0, 5.0, "SPEAKER_00", [
            ("hello", 0.0, 1.0),
        ])]
        Sanitizer().sanitize(segs)
        assert segs[0].end == 1.0


# ─── segment_level_only mode ─────────────────────────────────────────────

class TestSegmentOnlyMode:
    def test_word_passes_skipped(self):
        segs = [_seg(0.0, 10.0, "SPEAKER_00", [("hi", 0.0, 10.0)])]
        Sanitizer().sanitize_segment_only(segs)
        # Word duration NOT capped — only segment-level overlap fix runs
        assert segs[0].words[0].end == 10.0

    def test_same_speaker_segment_overlap_still_fixed(self):
        segs = [
            _seg(0.0, 2.0, "SPEAKER_00", [("a", 0.0, 2.0)]),
            _seg(1.5, 3.0, "SPEAKER_00", [("b", 1.5, 3.0)]),
        ]
        Sanitizer().sanitize_segment_only(segs)
        assert segs[0].end < segs[1].start


# ─── Custom policy ───────────────────────────────────────────────────────

class TestCustomPolicy:
    def test_silence_cap_override(self):
        # Tight 0.1 silence_cap — even the word "hello" (~0.6 estimate)
        # gets capped if duration > 0.7
        segs = [_seg(0.0, 5.0, "SPEAKER_00", [("hello", 0.0, 5.0)])]
        Sanitizer(TimingPolicy(silence_cap=0.1)).sanitize(segs)
        assert segs[0].words[0].end < 2.0

    def test_minimum_segment_duration_used_when_overlap_extreme(self):
        # Two same-speaker segments coincide at start
        segs = [
            _seg(0.0, 5.0, "SPEAKER_00", [("a", 0.0, 5.0)]),
            _seg(0.0, 5.0, "SPEAKER_00", [("b", 0.0, 5.0)]),
        ]
        Sanitizer(
            TimingPolicy(minimum_segment_duration=0.1)
        ).sanitize_segment_only(segs)
        # First segment is forced to at least 0.1s long
        assert segs[0].end >= segs[0].start + 0.05


# ─── Identical-start cluster redistribution ──────────────────────────────
#
# Regression coverage for the ElevenLabs Scribe v1 zero-duration cluster
# bug.  When Scribe collapses several CJK words onto one anchor timestamp
# (start == end across many consecutive words), the STT layer normalizes
# each word to a small positive duration (commit 1).  But that alone is
# not enough — every word still SHARES the same start, so the existing
# same-speaker overlap fix would later drag every neighbour's end back
# to the cluster anchor.  This pass redistributes such clusters linearly
# so each word has a distinct, monotonic start time before the overlap
# fix runs.

class TestClusterRedistribution:
    """Linearly redistribute words sharing an identical start time."""

    def test_cluster_with_distant_anchor_distributes_within_window(self):
        # 3 words at t=1.0, next real word at t=2.0
        # → cluster spread linearly across [1.0, 2.0]
        segs = [_seg(1.0, 2.0, "SPEAKER_00", [
            ("a", 1.0, 1.0),
            ("b", 1.0, 1.0),
            ("c", 1.0, 1.0),
            ("next", 2.0, 2.5),
        ])]
        Sanitizer().sanitize(segs)
        words = segs[0].words
        # Each cluster word must have distinct, monotonically increasing start
        assert words[0].start < words[1].start < words[2].start, (
            f"cluster not redistributed: starts="
            f"{[w.start for w in words[:3]]}"
        )
        # All cluster words end before the next real word starts
        for w in words[:3]:
            assert w.end <= words[3].start + 0.01

    def test_cluster_at_end_uses_floor_duration_per_word(self):
        # 3 words at t=5.0 with NO subsequent anchor
        # → fall back to anchor + i * floor
        segs = [_seg(5.0, 5.0, "SPEAKER_00", [
            ("a", 5.0, 5.0),
            ("b", 5.0, 5.0),
            ("c", 5.0, 5.0),
        ])]
        Sanitizer().sanitize(segs)
        words = segs[0].words
        assert words[0].start == 5.0
        assert words[1].start > words[0].start
        assert words[2].start > words[1].start

    def test_cluster_with_close_anchor_falls_back_to_floor(self):
        # 5 words at t=1.0, next word at t=1.01 (1ms gap, too tight)
        # → must fall back to floor-based spreading even though it
        # creates an overlap with the next word; segment-level overlap
        # fix will sort the rest out.
        segs = [_seg(1.0, 1.01, "SPEAKER_00", [
            ("a", 1.0, 1.0),
            ("b", 1.0, 1.0),
            ("c", 1.0, 1.0),
            ("d", 1.0, 1.0),
            ("e", 1.0, 1.0),
            ("close", 1.01, 1.5),
        ])]
        Sanitizer().sanitize(segs)
        cluster = segs[0].words[:5]
        starts = [w.start for w in cluster]
        # All cluster words must have distinct starts
        assert len(set(starts)) == 5, (
            f"close-anchor cluster not properly distributed: {starts}"
        )

    def test_solo_zero_duration_word_not_affected(self):
        # A single word with end already > start (after commit 1's floor)
        # should pass through untouched.
        segs = [_seg(3.0, 3.05, "SPEAKER_00", [("solo", 3.0, 3.05)])]
        Sanitizer().sanitize(segs)
        assert segs[0].words[0].start == 3.0
        assert segs[0].words[0].end == 3.05

    def test_distinct_start_words_unchanged(self):
        # Already well-formed words: pass-through.
        segs = [_seg(0.0, 2.0, "SPEAKER_00", [
            ("hello", 0.0, 0.5),
            ("world", 0.5, 1.0),
            ("today", 1.0, 1.5),
        ])]
        Sanitizer().sanitize(segs)
        for w_actual, w_expected in zip(
            segs[0].words,
            [(0.0, 0.5), (0.5, 1.0), (1.0, 1.5)],
        ):
            assert w_actual.start == pytest.approx(w_expected[0], abs=0.01)
            assert w_actual.end == pytest.approx(w_expected[1], abs=0.01)

    def test_cross_speaker_identical_start_not_clustered(self):
        # Two speakers happening to start at the exact same timestamp must
        # NOT be merged into a cluster — the overlap is a real interruption.
        segs = [
            _seg(1.0, 1.5, "SPEAKER_00", [("hi", 1.0, 1.5)]),
            _seg(1.0, 1.5, "SPEAKER_01", [("oh", 1.0, 1.5)]),
        ]
        Sanitizer().sanitize(segs)
        assert segs[0].words[0].start == 1.0
        assert segs[1].words[0].start == 1.0

    def test_mikovsgundam_cluster_no_collapse_to_zero(self):
        """End-to-end regression: 13 kanji on one anchor, mirroring
        the MIKOvsGUNDAM sample at t=6.41.

        The combined effect of cluster redistribution + same-speaker
        overlap fix must NOT shrink the segment back to zero duration.
        """
        kanji = list("使えるのかな？使えるのかな")  # 13 chars
        # All words share start=end=6.41 (post-STT-floor: end=6.46).
        # After cluster redistribution, every word should end up with a
        # distinct start, and the segment as a whole should have a
        # non-zero duration on the editor timeline.
        words = [(c, 6.41, 6.46) for c in kanji]
        segs = [_seg(6.41, 6.46, "SPEAKER_00", words)]
        Sanitizer().sanitize(segs)
        assert segs[0].end > segs[0].start, (
            f"segment collapsed to zero duration: "
            f"start={segs[0].start}, end={segs[0].end}"
        )
        starts = [w.start for w in segs[0].words]
        assert len(set(starts)) == len(starts), (
            f"cluster words still share starts: {starts}"
        )


# ─── Cluster stride cap + non-monotonic word ordering ────────────────────
#
# Regression coverage for the timestamp-breakage seen on jobs
# ba86a2ed51b2 ("Initial D" segment 232.5→248.1, "来た来た来た" repeats
# at 2:31): Scribe collapses several CJK kanji onto one anchor and the
# next real same-speaker word lies many seconds away. Without the
# stride cap, redistribution invents multi-second word durations and
# the segment-overlap pass propagates that into the rendered subtitle.
# Out-of-order word ordering after the translator's recheck pass can
# also drive the overlap-trim below seg.start, producing words with
# end < start.

class TestClusterStrideCap:
    """Cap per-word stride at TimingPolicy.duration_max."""

    def test_distant_anchor_does_not_inflate_stride_beyond_cap(self):
        """2 cluster words + a distant next-anchor (15 s away) must not
        produce ~7 s per word — the cap clips them at duration_max.
        """
        # Mirrors job ba86a2ed51b2 seg 114 ('Initial D'):
        # '文' and '字' both anchored at 232.59, next real word at 248.14.
        segs = [_seg(232.52, 248.14, "SPEAKER_00", [
            ("D", 232.520, 232.570),
            ("頭", 232.570, 232.590),
            ("文", 232.590, 232.590),
            ("字", 232.590, 232.590),
            ("井", 248.140, 248.340),
        ])]
        policy = TimingPolicy()
        Sanitizer(policy).sanitize(segs)
        cluster = segs[0].words[2:4]
        # Each redistributed cluster word's duration must respect the
        # normal-word cap. Without the fix, each was ~7.775 s.
        for w in cluster:
            dur = w.end - w.start
            assert dur <= policy.duration_max + 0.05, (
                f"cluster stride exceeded duration_max cap: word={w.word!r} "
                f"start={w.start} end={w.end} dur={dur}"
            )

    def test_close_anchor_still_uses_natural_stride(self):
        """When the natural even-stride is already within the cap, the
        cap must NOT tighten it — preserves prior behaviour for the
        common case (cluster fits comfortably inside the window).
        """
        # 3-kanji cluster, next anchor 1.0s away → even stride ≈ 0.33s
        # That's well below duration_max (1.5s), so it should pass.
        segs = [_seg(1.0, 2.0, "SPEAKER_00", [
            ("a", 1.0, 1.0),
            ("b", 1.0, 1.0),
            ("c", 1.0, 1.0),
            ("next", 2.0, 2.5),
        ])]
        Sanitizer().sanitize(segs)
        cluster = segs[0].words[:3]
        # Cluster fills (1.0, 2.0) ≈ 0.33s strides
        assert cluster[2].start <= 2.0 + 0.01
        assert cluster[2].start - cluster[0].start == pytest.approx(
            0.66, abs=0.05
        )

    def test_overlap_trim_never_pushes_end_below_start(self):
        """When two same-speaker words are out-of-order, trimming
        cur.end to nxt.start would drop below cur.start. The trim must
        clamp at cur.start (zero-duration anchor) instead of producing
        negative durations.
        """
        # Mirrors job ba86a2ed51b2 segs 111 + 113: 'く' word ends at
        # 232.400 but the next same-speaker word starts at 232.370.
        segs = [_seg(232.430, 232.500, "SPEAKER_00", [
            ("く", 232.430, 232.500),
        ]), _seg(232.370, 232.450, "SPEAKER_00", [
            ("井", 232.370, 232.450),
        ])]
        Sanitizer().sanitize(segs)
        # No word should end up with end < start
        for s in segs:
            for w in s.words:
                assert w.end >= w.start, (
                    f"negative-duration word: {w.word!r} "
                    f"start={w.start} end={w.end}"
                )

    def test_segment_overlap_trim_never_creates_negative_word_duration(self):
        """The segment-level overlap fix trims the last word's end too.
        It must clamp at the word's own start, not at the new (smaller)
        seg.end when seg.end < last_word.start.
        """
        # Construct the pathological case: cur seg ends well after nxt seg
        # starts, AND cur's last word starts AFTER nxt's start. Trimming
        # cur.end below the last word's start would create a negative
        # word duration.
        segs = [
            _seg(0.0, 5.0, "SPEAKER_00", [
                ("first", 0.0, 0.1),
                ("second", 4.9, 5.0),  # last word starts at 4.9
            ]),
            _seg(2.0, 3.0, "SPEAKER_00", [
                ("interrupt", 2.0, 3.0),
            ]),
        ]
        Sanitizer().sanitize(segs)
        # Find first segment by start time after sort
        first = next(s for s in segs if s.words and s.words[0].word == "first")
        for w in first.words:
            assert w.end >= w.start, (
                f"segment-overlap trim produced negative word duration: "
                f"{w.word!r} start={w.start} end={w.end}"
            )
