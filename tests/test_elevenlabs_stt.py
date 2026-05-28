"""Tests for processors.stt.elevenlabs — ElevenLabs Scribe response parsing.

The Scribe v1 API occasionally returns ``start == end`` for word entries,
particularly on CJK kanji runs.  We rely on the STT layer to normalize
these to a small positive duration before the rest of the pipeline runs,
so the sanitizer never has to invent a duration from nothing.
"""

from __future__ import annotations

from processors.stt.elevenlabs import ElevenLabsSttEngine


def _engine() -> ElevenLabsSttEngine:
    """Build an engine with a dummy API key so __init__ passes validation."""
    return ElevenLabsSttEngine(api_keys=["dummy-key"])


def _word(text: str, start: float, end: float, speaker: str = "speaker_0") -> dict:
    """Build a single word dict shaped like ElevenLabs Scribe response."""
    return {
        "type": "word",
        "text": text,
        "start": start,
        "end": end,
        "speaker_id": speaker,
    }


# ─── _flush_speaker_turn — defensive duration normalization ──────────────


class TestFlushSpeakerTurnNormalizesZeroDuration:
    """Regression tests for the ElevenLabs zero-duration word bug.

    Scribe v1 occasionally returns ``start == end`` for short CJK runs
    (single-mora kanji, expressive interjections).  Without normalization
    those zero-duration words pass straight through to the sanitizer,
    where ``_fix_same_speaker_word_overlaps`` then drags every neighbour's
    ``end`` back to the cluster's ``start`` — which collapses the entire
    surrounding segment to zero duration in the editor timeline.

    The fix is to give every word at least a tiny floor duration here so
    the sanitizer's later cluster-redistribution pass has something to
    work with.
    """

    def test_zero_duration_word_normalized_to_positive_end(self):
        # Single word with start == end (the broken Scribe shape)
        segments: list = []
        engine = _engine()
        engine._flush_speaker_turn(
            segments=segments,
            current_words=[_word("使", 6.41, 6.41)],
            current_speaker="speaker_0",
            speaker_detection=True,
        )
        assert len(segments) == 1
        word = segments[0].words[0]
        assert word.end > word.start, (
            f"zero-duration word not normalized: start={word.start}, "
            f"end={word.end} (expected end > start)"
        )

    def test_normalized_end_does_not_overshoot(self):
        # Floor duration should be small (≤ 100 ms) so we don't manufacture
        # a fake duration that crowds out the real next word.
        segments: list = []
        engine = _engine()
        engine._flush_speaker_turn(
            segments=segments,
            current_words=[_word("使", 6.41, 6.41)],
            current_speaker="speaker_0",
            speaker_detection=True,
        )
        word = segments[0].words[0]
        assert word.end - word.start <= 0.1, (
            f"floor duration too aggressive: {word.end - word.start}s"
        )

    def test_valid_duration_word_unchanged(self):
        # A word with a valid duration must pass through untouched —
        # we only synthesize when the data is broken.
        segments: list = []
        engine = _engine()
        engine._flush_speaker_turn(
            segments=segments,
            current_words=[_word("hello", 1.0, 1.5)],
            current_speaker="speaker_0",
            speaker_detection=True,
        )
        word = segments[0].words[0]
        assert word.start == 1.0
        assert word.end == 1.5

    def test_negative_duration_word_normalized(self):
        # Defense in depth: end < start would produce negative duration.
        segments: list = []
        engine = _engine()
        engine._flush_speaker_turn(
            segments=segments,
            current_words=[_word("oops", 2.0, 1.5)],
            current_speaker="speaker_0",
            speaker_detection=True,
        )
        word = segments[0].words[0]
        assert word.end > word.start

    def test_cjk_cluster_each_word_gets_positive_duration(self):
        # The MIKOvsGUNDAM scenario: 13 kanji at the same anchor t=6.41,
        # each with start==end.  After the STT-level normalization every
        # word should have end > start; the sanitizer pass added in
        # commit 2 will be responsible for redistributing them.
        cluster = [_word(c, 6.41, 6.41) for c in "使えるのかな？使えるのかな"]
        segments: list = []
        engine = _engine()
        engine._flush_speaker_turn(
            segments=segments,
            current_words=cluster,
            current_speaker="speaker_0",
            speaker_detection=True,
        )
        for w in segments[0].words:
            assert w.end > w.start, (
                f"cluster word not normalized: word='{w.word}' "
                f"start={w.start} end={w.end}"
            )


# ─── _parse_response — sentence-level segmentation ──────────────────────
#
# ElevenLabs Scribe API returns a flat list of words with no
# segment/utterance grouping.  The pre-fix grouper only flushed on
# (speaker change) OR (pause > 1s) OR (pause > 0.3s + previous word
# ends with ASCII .?!).  Result: monologues without long pauses came
# out as one giant segment regardless of how many sentences they
# contained — and Japanese punctuation (。？！) was never detected at
# all because they arrive as ``type=word`` standalone entries.
#
# These tests pin the new behaviour: split at sentence boundary
# unconditionally (ASCII + CJK) and cap segment length at 20 words
# as a safety net for run-on speech.


class TestParseResponseSentenceSplit:
    """Per-sentence segmentation in ``_parse_response``."""

    def test_ascii_period_splits_segment(self):
        engine = _engine()
        # Two sentences, no long pause between them — must still split.
        raw = {"words": [
            _word("Hello", 0.0, 0.5),
            _word("world", 0.5, 1.0),
            {"type": "punctuation", "text": ".", "start": 1.0, "end": 1.0,
             "speaker_id": "speaker_0"},
            _word("How", 1.05, 1.4),
            _word("are", 1.4, 1.7),
            _word("you", 1.7, 2.0),
            {"type": "punctuation", "text": "?", "start": 2.0, "end": 2.0,
             "speaker_id": "speaker_0"},
        ]}
        segments = engine._parse_response(raw, speaker_detection=True)
        assert len(segments) == 2, (
            f"ASCII sentence boundary not detected: {len(segments)} segments"
        )
        assert "Hello" in segments[0].text and "world" in segments[0].text
        assert "How" in segments[1].text and "you" in segments[1].text

    def test_japanese_period_splits_segment(self):
        engine = _engine()
        # Japanese 。 arrives as type=word standalone.  Without explicit
        # handling it stays attached to the cluster as a separate entry
        # and the segmenter never recognises a sentence boundary.
        raw = {"words": [
            _word("確", 6.41, 6.44),
            _word("か", 6.44, 6.54),
            _word("に", 6.54, 6.72),
            _word("。", 6.72, 6.72),
            _word("そ", 6.78, 6.9),
            _word("し", 6.9, 6.98),
            _word("た", 6.98, 7.18),
            _word("ら", 7.18, 7.22),
        ]}
        segments = engine._parse_response(raw, speaker_detection=True)
        assert len(segments) == 2, (
            f"Japanese 。 not detected as sentence boundary: "
            f"{len(segments)} segments"
        )
        assert "確" in segments[0].text and "に" in segments[0].text
        assert "そ" in segments[1].text and "ら" in segments[1].text

    def test_japanese_question_mark_splits_segment(self):
        engine = _engine()
        raw = {"words": [
            _word("使", 6.41, 6.46),
            _word("え", 6.46, 6.51),
            _word("る", 6.51, 6.56),
            _word("？", 6.56, 6.56),
            _word("確", 6.6, 6.65),
            _word("か", 6.65, 6.7),
        ]}
        segments = engine._parse_response(raw, speaker_detection=True)
        assert len(segments) == 2

    def test_japanese_exclamation_splits_segment(self):
        engine = _engine()
        raw = {"words": [
            _word("う", 0.0, 0.1),
            _word("わ", 0.1, 0.2),
            _word("！", 0.2, 0.2),
            _word("や", 0.3, 0.4),
            _word("ば", 0.4, 0.5),
            _word("い", 0.5, 0.6),
        ]}
        segments = engine._parse_response(raw, speaker_detection=True)
        assert len(segments) == 2

    def test_max_word_cap_forces_split(self):
        # 30 words, no punctuation, no pause — segmenter must cap the run.
        engine = _engine()
        raw = {"words": [
            _word(f"w{i}", i * 0.1, (i + 1) * 0.1) for i in range(30)
        ]}
        segments = engine._parse_response(raw, speaker_detection=True)
        # With a 20-word cap, 30 words → at least 2 segments
        assert len(segments) >= 2, (
            f"max-word cap not enforced: {len(segments)} segment(s) for 30 words"
        )
        for s in segments:
            assert len(s.words) <= 20, (
                f"segment exceeds 20-word cap: {len(s.words)} words"
            )

    def test_speaker_change_still_splits(self):
        # Regression: speaker change must still trigger a split independent
        # of sentence boundary detection.
        engine = _engine()
        raw = {"words": [
            _word("hello", 0.0, 0.5, speaker="speaker_0"),
            _word("world", 0.5, 1.0, speaker="speaker_0"),
            _word("hi", 1.5, 2.0, speaker="speaker_1"),
        ]}
        segments = engine._parse_response(raw, speaker_detection=True)
        assert len(segments) == 2
        assert segments[0].speaker == "SPEAKER_00"
        assert segments[1].speaker == "SPEAKER_01"

    def test_long_pause_still_splits(self):
        # Regression: pause > 1s must still trigger a split.
        engine = _engine()
        raw = {"words": [
            _word("hello", 0.0, 0.5),
            _word("world", 0.5, 1.0),
            _word("again", 5.0, 5.5),
        ]}
        segments = engine._parse_response(raw, speaker_detection=True)
        assert len(segments) == 2

    def test_short_unpunctuated_run_stays_together(self):
        # Counterpoint: <20 words, no terminator, no pause → one segment.
        engine = _engine()
        raw = {"words": [
            _word(f"w{i}", i * 0.1, (i + 1) * 0.1) for i in range(5)
        ]}
        segments = engine._parse_response(raw, speaker_detection=True)
        assert len(segments) == 1

    def test_mikovsgundam_first_segment_splits_per_sentence(self):
        # End-to-end sample mirroring the screenshot: 4 Japanese sentences
        # separated by 。 and ？ should produce 4 segments, not 1.
        engine = _engine()
        sentences = [
            ("使えるのかな？", 6.41),
            ("確かに。", 6.5),
            ("そしたらバンブルビーと戦おうかな。", 6.78),
            ("トランスフォーマー。", 8.5),
        ]
        words = []
        t = 0.0
        for sentence, t_start in sentences:
            t = t_start
            for ch in sentence:
                words.append(_word(ch, round(t, 3), round(t + 0.05, 3)))
                t += 0.06
        raw = {"words": words}
        segments = engine._parse_response(raw, speaker_detection=True)
        assert len(segments) == 4, (
            f"expected 4 sentence-level segments, got {len(segments)}"
        )


# ─── Sprint 1: per-word confidence wiring ───────────────────────────────


class TestConfidenceFromLogprob:
    """Wire ElevenLabs ``logprob`` field through to ``WordTimestamp.score``.

    Scribe v2 returns ``logprob`` in ``[-inf, 0]`` per word.  Mapping
    ``score = exp(logprob)`` produces a 0..1 probability that the
    Preview UI can use to flag low-confidence words for review.  This
    replaces the pre-Sprint-1 hardcoded ``score=1.0``.
    """

    def test_zero_logprob_maps_to_full_confidence(self):
        # logprob == 0 means probability == 1 (model perfectly certain).
        assert ElevenLabsSttEngine._confidence_from_logprob(0.0) == 1.0

    def test_negative_logprob_maps_to_partial_confidence(self):
        # logprob == -1.16 (sample value from MIKOvsGUNDAM) → ~0.31.
        score = ElevenLabsSttEngine._confidence_from_logprob(-1.1585392951965332)
        assert 0.30 < score < 0.32, f"unexpected score {score}"

    def test_missing_logprob_defaults_to_one(self):
        # Legacy responses + audio_event entries don't carry logprob.
        # We must not flag those as low confidence.
        assert ElevenLabsSttEngine._confidence_from_logprob(None) == 1.0

    def test_garbage_logprob_falls_back_to_one(self):
        # Defensive: malformed payloads must not crash the pipeline.
        assert ElevenLabsSttEngine._confidence_from_logprob("nonsense") == 1.0

    def test_score_propagates_through_flush(self):
        # Integration: a word with logprob in the API payload should
        # land on the ``WordTimestamp.score`` field.
        engine = _engine()
        segments: list = []
        word_payload = _word("hello", 1.0, 1.5)
        word_payload["logprob"] = -0.6931471805599453  # ln(0.5)
        engine._flush_speaker_turn(
            segments=segments,
            current_words=[word_payload],
            current_speaker="speaker_0",
            speaker_detection=True,
        )
        assert 0.49 < segments[0].words[0].score < 0.51


# ─── Sprint 1: config-driven Scribe payload ─────────────────────────────


class TestPayloadHonoursConfig:
    """The HTTP payload must reflect ``config.ELEVENLABS_*`` knobs.

    Patch ``config`` directly rather than spawn a real httpx call.  We
    only need to verify that the data-dict assembly inside ``_call_api``
    picks up the right values; the network round-trip is covered by
    integration tests outside the pytest suite.
    """

    def _capture_payload(self, monkeypatch) -> dict:
        """Patch httpx so the post is intercepted and the form-data captured."""
        captured: dict = {}

        class _FakeResponse:
            status_code = 200

            def json(self):  # noqa: D401 — match httpx API
                return {"words": [], "text": ""}

        class _FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, url, headers=None, files=None, data=None):
                captured["url"] = url
                captured["headers"] = headers
                captured["data"] = dict(data or {})
                return _FakeResponse()

        import processors.stt.elevenlabs as stt_mod
        monkeypatch.setattr(stt_mod.httpx, "AsyncClient", _FakeClient)
        return captured

    async def test_payload_uses_config_model(self, monkeypatch, tmp_path):
        # Verify model_id, temperature, and seed all come from config.
        from processors.stt import elevenlabs as stt_mod
        monkeypatch.setattr(stt_mod.config, "ELEVENLABS_STT_MODEL", "scribe_v2")
        monkeypatch.setattr(stt_mod.config, "ELEVENLABS_STT_TEMPERATURE", 0.0)
        monkeypatch.setattr(stt_mod.config, "ELEVENLABS_STT_SEED", "42")
        monkeypatch.setattr(stt_mod.config, "ELEVENLABS_NO_VERBATIM", False)
        captured = self._capture_payload(monkeypatch)

        audio = tmp_path / "fake.wav"
        audio.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")
        await _engine()._call_api(audio, diarize=True)

        data = captured["data"]
        assert data["model_id"] == "scribe_v2"
        assert data["temperature"] == "0.0"
        assert data["seed"] == "42"
        assert "no_verbatim" not in data, "no_verbatim must stay opt-in"

    async def test_no_verbatim_only_when_v2_and_enabled(
        self, monkeypatch, tmp_path,
    ):
        # Sending no_verbatim=true to scribe_v1 returns 400.  The engine
        # must suppress the flag when the active model is v1 even if the
        # env knob is set — defense in depth against accidental config.
        from processors.stt import elevenlabs as stt_mod
        monkeypatch.setattr(stt_mod.config, "ELEVENLABS_STT_MODEL", "scribe_v1")
        monkeypatch.setattr(stt_mod.config, "ELEVENLABS_NO_VERBATIM", True)
        monkeypatch.setattr(stt_mod.config, "ELEVENLABS_STT_TEMPERATURE", 0.0)
        monkeypatch.setattr(stt_mod.config, "ELEVENLABS_STT_SEED", "")
        captured = self._capture_payload(monkeypatch)

        audio = tmp_path / "fake.wav"
        audio.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")
        await _engine()._call_api(audio, diarize=True)

        assert "no_verbatim" not in captured["data"], (
            "no_verbatim leaked into a scribe_v1 request"
        )

    async def test_language_code_pass_through(self, monkeypatch, tmp_path):
        # When the caller hints a language, it lands on the wire.
        from processors.stt import elevenlabs as stt_mod
        monkeypatch.setattr(stt_mod.config, "ELEVENLABS_STT_MODEL", "scribe_v2")
        monkeypatch.setattr(stt_mod.config, "ELEVENLABS_STT_TEMPERATURE", 0.0)
        monkeypatch.setattr(stt_mod.config, "ELEVENLABS_STT_SEED", "")
        monkeypatch.setattr(stt_mod.config, "ELEVENLABS_NO_VERBATIM", False)
        captured = self._capture_payload(monkeypatch)

        audio = tmp_path / "fake.wav"
        audio.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")
        await _engine()._call_api(audio, diarize=True, language_code="jpn")

        assert captured["data"].get("language_code") == "jpn"
