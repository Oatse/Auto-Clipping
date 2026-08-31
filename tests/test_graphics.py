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
    """Answers the track-preparation call, then one call per batch."""

    def __init__(self, fail_on_batch: int | None = None):
        self.scripts: list[str] = []
        self.fail_on_batch = fail_on_batch
        self._batches = 0

    def execute(self, code, *, timeout=None):
        self.scripts.append(code)
        if "addTracks" in code and "importMGT" not in code:
            return BridgeResponse(
                success=True, data={"baseTrack": 3, "available": 5}
            )
        self._batches += 1
        if self.fail_on_batch == self._batches:
            return BridgeResponse.failed("Premiere stopped responding")
        placed = code.count('"s":') or code.count('"s": ')
        return BridgeResponse(success=True, data={"placed": placed, "failed": 0})

    @property
    def batch_scripts(self) -> list[str]:
        return [s for s in self.scripts if "importMGT" in s]


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

    def test_work_is_split_into_batches(self, tmp_path):
        # Every importMGT re-reads the template from disk, and a few hundred
        # in one blocking call crashed Premiere. Batching is a stability
        # requirement, not an optimisation.
        bridge = FakeBridge()
        import_as_graphics(
            bridge,
            [_seg(i * 2, i * 2 + 1, f"line {i}") for i in range(50)],
            template=self._template(tmp_path),
            batch_size=20, limit=None,
        )
        assert len(bridge.batch_scripts) == 3        # 20 + 20 + 10

    def test_tracks_are_added_once_not_per_batch(self, tmp_path):
        bridge = FakeBridge()
        import_as_graphics(
            bridge,
            [_seg(i * 2, i * 2 + 1, f"l{i}") for i in range(40)],
            template=self._template(tmp_path), batch_size=10,
        )
        assert sum(1 for s in bridge.scripts if "addTracks" in s) == 1

    def test_a_failed_batch_stops_and_reports_progress(self, tmp_path):
        # Better to stop than keep hammering an unhappy Premiere, and the
        # partial count is what makes the result understandable.
        bridge = FakeBridge(fail_on_batch=2)
        r = import_as_graphics(
            bridge,
            [_seg(i * 2, i * 2 + 1, f"l{i}") for i in range(30)],
            template=self._template(tmp_path), batch_size=10,
        )
        assert r.success is False
        assert "batch 2 of 3" in r.error
        assert "10 graphic" in r.error

    def test_refuses_an_unsafe_volume(self, tmp_path):
        # The load that took Premiere down should not be attempted silently.
        bridge = FakeBridge()
        r = import_as_graphics(
            bridge,
            [_seg(i * 2, i * 2 + 1, f"l{i}") for i in range(60)],
            template=self._template(tmp_path), limit=50,
        )
        assert r.success is False
        assert "Upgrade Caption to Graphic" in r.error
        assert bridge.scripts == []                  # nothing attempted

    def test_totals_are_summed_across_batches(self, tmp_path):
        bridge = FakeBridge()
        r = import_as_graphics(
            bridge,
            [_seg(i * 2, i * 2 + 1, f"l{i}") for i in range(25)],
            template=self._template(tmp_path), batch_size=10,
        )
        assert r.success
        assert r.data["placed"] == 25
        assert r.data["batches"] == 3

    def test_script_uses_the_verified_api(self, tmp_path):
        bridge = FakeBridge()
        import_as_graphics(
            bridge, [_seg(0, 1, "hello")], template=self._template(tmp_path),
        )
        batch = bridge.batch_scripts[0]
        assert "importMGT" in batch
        assert "setValue" in batch
        assert "clip.end" in batch
        assert any("addTracks" in s for s in bridge.scripts)

    def test_cue_data_is_embedded_as_json(self, tmp_path):
        bridge = FakeBridge()
        import_as_graphics(
            bridge, [_seg(1.5, 3.0, "hello")], template=self._template(tmp_path),
        )
        script = bridge.batch_scripts[0]
        assert '"t": "hello"' in script or '"t":"hello"' in script
        assert "1.5" in script and "3.0" in script

    def test_unicode_survives(self, tmp_path):
        bridge = FakeBridge()
        import_as_graphics(
            bridge, [_seg(0, 1, "こんにちは")], template=self._template(tmp_path),
        )
        assert "こんにちは" in bridge.batch_scripts[0]

    def test_quotes_in_text_do_not_break_the_script(self, tmp_path):
        bridge = FakeBridge()
        import_as_graphics(
            bridge, [_seg(0, 1, 'she said "no" \\ then left')],
            template=self._template(tmp_path),
        )
        script = bridge.batch_scripts[0]
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
        prepare = next(s for s in bridge.scripts if "addTracks" in s)
        assert "addTracks(2," in prepare

    def test_ticks_constant_is_premieres(self, tmp_path):
        bridge = FakeBridge()
        import_as_graphics(
            bridge, [_seg(0, 1, "a")], template=self._template(tmp_path),
        )
        assert str(TICKS_PER_SECOND) in bridge.batch_scripts[0]
