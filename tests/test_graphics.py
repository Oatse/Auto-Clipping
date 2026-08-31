"""Tests for placing captions as Essential Graphics clips.

The point of graphics over a caption track is twofold: the text stays freely
editable, and because each cue is an ordinary clip, overlapping speech can be
laid on separate tracks and actually shown together — which a caption track,
and SRT itself, cannot do.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from models.transcript import TranscriptSegment
from processors.premiere import graphics as gfx
from processors.premiere.bridge_client import BridgeResponse
from processors.premiere.graphics import (
    TICKS_PER_SECOND,
    assign_lanes,
    find_caption_template,
    import_as_graphics,
)


def _seg(start, end, text, speaker="SPEAKER_00"):
    return TranscriptSegment(start=start, end=end, text=text, speaker=speaker)


class FakeBridge:
    def __init__(self, response=None):
        self.scripts: list[str] = []
        self.response = response or BridgeResponse(
            success=True, data={"placed": 0, "failed": 0, "tracks": 1}
        )

    def execute(self, code, *, timeout=None):
        self.scripts.append(code)
        return self.response


# ─── Lane assignment ─────────────────────────────────────────────────────────

class TestAssignLanes:
    def test_sequential_speech_stays_on_one_lane(self):
        # The common case: no reason to spend a second track on it.
        cues = assign_lanes([_seg(0, 1, "a"), _seg(2, 3, "b"), _seg(4, 5, "c")])
        assert {c.lane for c in cues} == {0}

    def test_overlap_moves_to_a_second_lane(self):
        cues = assign_lanes([_seg(0, 3, "a"), _seg(1, 4, "b")])
        assert [c.lane for c in cues] == [0, 1]

    def test_lane_is_reused_once_free(self):
        cues = assign_lanes([
            _seg(0, 3, "a"), _seg(1, 4, "b"), _seg(5, 6, "c"),
        ])
        assert [c.lane for c in cues] == [0, 1, 0]

    def test_three_way_overlap_uses_three_lanes(self):
        cues = assign_lanes([
            _seg(0, 5, "a"), _seg(1, 5, "b"), _seg(2, 5, "c"),
        ])
        assert [c.lane for c in cues] == [0, 1, 2]

    def test_lane_count_is_capped(self):
        # Growing the sequence without bound would be worse than stacking.
        cues = assign_lanes(
            [_seg(0, 10, f"s{i}") for i in range(8)], max_lanes=3,
        )
        assert max(c.lane for c in cues) == 2

    def test_zero_length_cue_gets_duration(self):
        cues = assign_lanes([_seg(5, 5, "instant")])
        assert cues[0].end > cues[0].start

    def test_empty_text_is_dropped(self):
        cues = assign_lanes([_seg(0, 1, "  "), _seg(1, 2, "real")])
        assert [c.text for c in cues] == ["real"]

    def test_cues_are_time_ordered(self):
        cues = assign_lanes([_seg(5, 6, "late"), _seg(0, 1, "early")])
        assert [c.text for c in cues] == ["early", "late"]


# ─── Template discovery ──────────────────────────────────────────────────────

class TestTemplateDiscovery:
    def test_explicit_path_wins(self, tmp_path):
        t = tmp_path / "custom.mogrt"
        t.write_text("x")
        assert find_caption_template(str(t)) == t

    def test_env_var_is_honoured(self, tmp_path, monkeypatch):
        t = tmp_path / "env.mogrt"
        t.write_text("x")
        monkeypatch.setenv("PREMIERE_CAPTION_MOGRT", str(t))
        assert find_caption_template() == t

    def test_found_under_captions_folder(self, tmp_path, monkeypatch):
        monkeypatch.delenv("PREMIERE_CAPTION_MOGRT", raising=False)
        folder = tmp_path / "Premiere 2099" / "Essential Graphics" / "Captions and Subtitles"
        folder.mkdir(parents=True)
        (folder / gfx.DEFAULT_TEMPLATE_NAME).write_text("x")
        monkeypatch.setattr(gfx, "_TEMPLATE_ROOTS", (str(tmp_path),))
        assert find_caption_template().name == gfx.DEFAULT_TEMPLATE_NAME

    def test_falls_back_to_any_caption_template(self, tmp_path, monkeypatch):
        monkeypatch.delenv("PREMIERE_CAPTION_MOGRT", raising=False)
        folder = tmp_path / "Essential Graphics" / "Captions and Subtitles"
        folder.mkdir(parents=True)
        (folder / "Bold Web Caption.mogrt").write_text("x")
        monkeypatch.setattr(gfx, "_TEMPLATE_ROOTS", (str(tmp_path),))
        assert find_caption_template().name == "Bold Web Caption.mogrt"


# ─── Script generation ───────────────────────────────────────────────────────

class TestImportAsGraphics:
    def _template(self, tmp_path):
        t = tmp_path / "caption.mogrt"
        t.write_text("x")
        return t

    def test_reports_missing_template(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gfx, "find_caption_template", lambda *a, **k: None)
        r = import_as_graphics(FakeBridge(), [_seg(0, 1, "a")])
        assert r.success is False
        assert "PREMIERE_CAPTION_MOGRT" in r.error

    def test_refuses_empty_input(self, tmp_path):
        r = import_as_graphics(
            FakeBridge(), [], template=self._template(tmp_path),
        )
        assert r.success is False

    def test_sends_one_call_for_all_cues(self, tmp_path):
        # One round trip: per-cue calls would each wait on the connector's
        # 200 ms poll, turning a few hundred captions into minutes.
        bridge = FakeBridge()
        import_as_graphics(
            bridge,
            [_seg(i, i + 0.5, f"line {i}") for i in range(50)],
            template=self._template(tmp_path),
        )
        assert len(bridge.scripts) == 1

    def test_script_uses_the_verified_api(self, tmp_path):
        bridge = FakeBridge()
        import_as_graphics(
            bridge, [_seg(0, 1, "hello")], template=self._template(tmp_path),
        )
        script = bridge.scripts[0]
        assert "importMGT" in script
        assert "setValue" in script
        assert "clip.end" in script
        assert "addTracks" in script

    def test_cue_data_is_embedded_as_json(self, tmp_path):
        bridge = FakeBridge()
        import_as_graphics(
            bridge, [_seg(1.5, 3.0, "hello")], template=self._template(tmp_path),
        )
        script = bridge.scripts[0]
        assert '"t": "hello"' in script or '"t":"hello"' in script
        assert "1.5" in script and "3.0" in script

    def test_unicode_survives(self, tmp_path):
        bridge = FakeBridge()
        import_as_graphics(
            bridge, [_seg(0, 1, "こんにちは")], template=self._template(tmp_path),
        )
        assert "こんにちは" in bridge.scripts[0]

    def test_quotes_in_text_do_not_break_the_script(self, tmp_path):
        bridge = FakeBridge()
        import_as_graphics(
            bridge, [_seg(0, 1, 'she said "no" \\ then left')],
            template=self._template(tmp_path),
        )
        script = bridge.scripts[0]
        # The cue array must remain parseable JSON.
        start = script.index("var CUES = ") + len("var CUES = ")
        end = script.index("\nvar MOGRT")
        json.loads(script[start:end].strip().rstrip(";"))

    def test_track_count_matches_lanes_used(self, tmp_path):
        bridge = FakeBridge()
        import_as_graphics(
            bridge, [_seg(0, 3, "a"), _seg(1, 4, "b")],
            template=self._template(tmp_path),
        )
        assert "var LANES = 2;" in bridge.scripts[0]

    def test_ticks_constant_is_premieres(self, tmp_path):
        bridge = FakeBridge()
        import_as_graphics(
            bridge, [_seg(0, 1, "a")], template=self._template(tmp_path),
        )
        assert str(TICKS_PER_SECOND) in bridge.scripts[0]
