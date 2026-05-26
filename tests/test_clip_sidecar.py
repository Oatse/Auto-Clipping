"""Tests for processors.clip_finder.clip_sidecar."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from processors.clip_finder.clip_sidecar import (
    ClipSidecar,
    _default_sidecar,
    _flatten_transcript,
    _parse_response,
    read,
    write,
)


# ─── ClipSidecar shape ───────────────────────────────────────────────────────


class TestClipSidecarShape:
    def test_to_dict_caps_title_at_70(self):
        sc = ClipSidecar(title="x" * 200, description="d")
        assert len(sc.to_dict()["title"]) == 70

    def test_to_dict_caps_description_at_280(self):
        sc = ClipSidecar(title="t", description="d" * 500)
        assert len(sc.to_dict()["description"]) == 280

    def test_to_dict_strips_hashtag_prefix(self):
        sc = ClipSidecar(title="t", description="d", hashtags=["#funny", "real "])
        assert sc.to_dict()["hashtags"] == ["funny", "real"]

    def test_to_dict_drops_empty_hashtags(self):
        sc = ClipSidecar(title="t", description="d", hashtags=["", "  ", "good"])
        assert sc.to_dict()["hashtags"] == ["good"]

    def test_from_dict_round_trip(self):
        original = ClipSidecar(
            title="Hook",
            description="Tight punchline at 12s",
            hashtags=["clip", "shorts"],
            suggested_thumbnail_t=4.2,
            emoji_hint="🔥",
            language="id",
        )
        restored = ClipSidecar.from_dict(original.to_dict())
        assert restored.title == "Hook"
        assert restored.description == "Tight punchline at 12s"
        assert restored.hashtags == ["clip", "shorts"]
        assert restored.suggested_thumbnail_t == pytest.approx(4.2)
        assert restored.emoji_hint == "🔥"
        assert restored.language == "id"


# ─── Response parsing ────────────────────────────────────────────────────────


class TestResponseParsing:
    def test_parses_clean_json(self):
        text = '{"title": "x", "description": "y"}'
        assert _parse_response(text) == {"title": "x", "description": "y"}

    def test_strips_code_fences(self):
        text = '```json\n{"title": "x"}\n```'
        assert _parse_response(text) == {"title": "x"}

    def test_salvages_from_surrounding_prose(self):
        text = 'Sure! Here you go:\n{"title": "x"}\nLet me know.'
        assert _parse_response(text) == {"title": "x"}

    def test_empty_returns_empty_dict(self):
        assert _parse_response("") == {}

    def test_garbage_returns_empty_dict(self):
        assert _parse_response("not json at all") == {}


# ─── Helpers ─────────────────────────────────────────────────────────────────


class TestFlattenTranscript:
    def test_string_passthrough(self):
        assert _flatten_transcript("hello world") == "hello world"

    def test_dict_segments_joined(self):
        window = [
            {"start": 0.0, "end": 1.0, "text": "first"},
            {"start": 1.0, "end": 2.0, "text": "second"},
        ]
        assert _flatten_transcript(window) == "first\nsecond"

    def test_empty_segments_skipped(self):
        window = [
            {"text": "kept"},
            {"text": ""},
            {"text": "  "},
        ]
        assert _flatten_transcript(window) == "kept"

    def test_empty_input_returns_empty(self):
        assert _flatten_transcript([]) == ""
        assert _flatten_transcript("") == ""


class TestDefaultSidecar:
    def test_uses_working_title(self):
        sc = _default_sidecar("My Clip", "A nice moment", 30.0)
        assert sc.title == "My Clip"
        assert sc.description == "A nice moment"

    def test_thumbnail_at_40_percent(self):
        sc = _default_sidecar("t", "d", 30.0)
        assert sc.suggested_thumbnail_t == pytest.approx(12.0)

    def test_falls_back_to_clip_when_title_empty(self):
        sc = _default_sidecar("", "", 10.0)
        assert sc.title == "Clip"


# ─── Write/read round trip ───────────────────────────────────────────────────


class TestWriteRead:
    def test_write_creates_sidecar_file(self, tmp_path: Path):
        clip = tmp_path / "clip_001_test.mp4"
        clip.write_bytes(b"fake video bytes")
        sc = ClipSidecar(title="Hook", description="Pitch")
        sidecar_path = write(sc, clip)
        assert sidecar_path.exists()
        assert sidecar_path.name == "clip_001_test.metadata.json"

    def test_read_round_trip(self, tmp_path: Path):
        clip = tmp_path / "clip_002.mp4"
        clip.write_bytes(b"x")
        original = ClipSidecar(
            title="T",
            description="D",
            hashtags=["a", "b"],
            suggested_thumbnail_t=3.5,
        )
        write(original, clip)
        loaded = read(clip)
        assert loaded is not None
        assert loaded.title == "T"
        assert loaded.hashtags == ["a", "b"]
        assert loaded.suggested_thumbnail_t == pytest.approx(3.5)

    def test_read_missing_returns_none(self, tmp_path: Path):
        clip = tmp_path / "clip_xx.mp4"
        clip.write_bytes(b"x")
        assert read(clip) is None

    def test_read_corrupt_returns_none(self, tmp_path: Path):
        clip = tmp_path / "clip_yy.mp4"
        clip.write_bytes(b"x")
        sidecar = clip.with_suffix(".metadata.json")
        sidecar.write_text("{not: valid json", encoding="utf-8")
        assert read(clip) is None
