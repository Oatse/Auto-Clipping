"""Unit tests for chat clip-intent mining.

Chat typing "clip it" is the highest-precision clip-worthiness signal
available for a VTuber VOD — the audience nominates the moment itself.
These tests pin both halves of that claim: that the patterns fire on
real phrasings across the three languages the audience actually uses,
and that they stay quiet on the phrasings that would poison the signal
(most importantly "clip" meaning a magazine in shooter streams).
"""

from __future__ import annotations

from models.clip import SignalKind
from processors.clip_finder.chat_signals import (
    CLIP_INTENT_MIN,
    ChatSignalExtractor,
    WINDOW_SECONDS,
    _is_clip_intent,
)


def _msg(t: float, text: str) -> dict:
    return {"t": t, "type": "text", "text": text, "emotes": [], "is_super": False}


class TestClipIntentPatterns:
    def test_matches_english_requests(self):
        for text in (
            "clip it",
            "CLIP THAT",
            "someone clip this",
            "pls clip this",
            "that needs a clip",
            "clip worthy",
            "clipping this rn",
            "already clipped",
            "CLIP!!!",
        ):
            assert _is_clip_intent(text), text

    def test_matches_japanese_and_indonesian(self):
        assert _is_clip_intent("切り抜き待ってる")
        assert _is_clip_intent("きりぬきはよ")
        assert _is_clip_intent("クリップにして")
        assert _is_clip_intent("klip ini wajib")

    def test_ignores_magazine_clip_and_other_false_friends(self):
        """A bare 'clip' is not intent — in shooters it means a magazine."""
        for text in (
            "reload your clip",
            "she is out of clips",
            "nice clip art lol",
            "clipboard broke",
            "",
        ):
            assert not _is_clip_intent(text), text


class TestClipIntentEvents:
    def test_single_request_does_not_emit(self):
        """One stray message must not create a signal on its own."""
        messages = [_msg(10.0, "clip it")]
        assert ChatSignalExtractor._compute_clip_intent(messages) == []

    def test_burst_emits_event_with_quotes(self):
        messages = [
            _msg(10.0, "clip it"),
            _msg(11.0, "CLIP THAT"),
            _msg(12.0, "切り抜き頼む"),
            _msg(13.0, "just chatting"),   # noise, must not count
        ]
        events = ChatSignalExtractor._compute_clip_intent(messages)
        assert len(events) == 1
        ev = events[0]
        assert ev.kind == SignalKind.CHAT_CLIP_INTENT
        assert ev.start == (10.0 // WINDOW_SECONDS) * WINDOW_SECONDS
        assert ev.end == ev.start + WINDOW_SECONDS
        assert "3x" in ev.label
        # The quoted text is what lets the scoring prompt tell a real
        # nomination from a coincidence, so it must survive.
        assert "clip it" in ev.sample

    def test_intensity_saturates(self):
        """Past a point, more requests carry no extra information."""
        messages = [_msg(10.0 + i * 0.1, "clip it") for i in range(40)]
        events = ChatSignalExtractor._compute_clip_intent(messages)
        assert len(events) == 1
        assert events[0].intensity == 1.0

    def test_min_threshold_is_respected(self):
        messages = [_msg(10.0 + i * 0.1, "clip it") for i in range(CLIP_INTENT_MIN - 1)]
        assert ChatSignalExtractor._compute_clip_intent(messages) == []

    def test_separate_windows_emit_separate_events(self):
        messages = [
            _msg(2.0, "clip it"), _msg(3.0, "clip that"),
            _msg(600.0, "clip it"), _msg(601.0, "clip this"),
        ]
        events = ChatSignalExtractor._compute_clip_intent(messages)
        assert len(events) == 2
        assert events[0].start < events[1].start


class TestChatSpikeQuotes:
    def test_spike_carries_what_chat_said(self):
        """A spike of greetings and a spike of shock must look different."""
        # Flat baseline, then a burst of one repeated message.
        messages = [_msg(float(i) * 5.0, "hi") for i in range(40)]
        messages += [_msg(100.0 + i * 0.05, "SHE DID NOT JUST SAY THAT")
                     for i in range(60)]
        spikes = ChatSignalExtractor._compute_velocity_spikes(messages)
        assert spikes, "expected a velocity spike from the burst"
        hot = max(spikes, key=lambda e: e.intensity)
        assert "SHE DID NOT JUST SAY THAT" in hot.sample
