"""Tests for the Premiere handoff: media probing + FCP7 XML generation.

The timeline is only useful if it is frame-accurate against the master and
Premiere can actually relink the media. Both are easy to break silently, so
they are pinned here:

  * frame math uses the exact rational rate (29.97 is 30000/1001, not 30) —
    rounding drifts ~7 s across a two-hour VOD;
  * broadcast rates are written as integer timebase + ntsc flag;
  * ``pathurl`` keeps the drive-letter colon literal, since a percent-
    encoded ``D%3A`` cannot be relinked.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path

import pytest

from models.clip import Clip, ClipScore, HighlightType
from processors.premiere.fcpxml import (
    build_fcpxml,
    path_to_url,
    seconds_to_frames,
    write_fcpxml,
)
from processors.premiere.source import MediaInfo, probe_media


def _clip(start: float, end: float, title: str = "m", reason: str = "why") -> Clip:
    return Clip(
        start=start,
        end=end,
        title=title,
        reason=reason,
        highlight_type=HighlightType.COLLAB_DYNAMIC,
        score=ClipScore(quotability=8.0),
        score_profile="vtuber",
    )


def _master(fps: Fraction = Fraction(30, 1)) -> MediaInfo:
    return MediaInfo(
        path=Path("D:/WORK/CLIP-AUTOMATION/out/master.mp4"),
        fps=fps,
        width=1920,
        height=1080,
        duration=7200.0,
    )


def _parse(xml: str) -> ET.Element:
    return ET.fromstring(xml[xml.index("<xmeml"):])


# ─── Frame math ──────────────────────────────────────────────────────────────

class TestFrameMath:
    def test_whole_rate_conversion(self):
        assert seconds_to_frames(100.0, Fraction(30, 1)) == 3000
        assert seconds_to_frames(0.0, Fraction(30, 1)) == 0

    def test_negative_clamps_to_zero(self):
        assert seconds_to_frames(-5.0, Fraction(30, 1)) == 0

    def test_ntsc_rate_is_exact_not_rounded(self):
        # Two hours at 29.97: exact math gives 215784 frames. Treating the
        # rate as a flat 30 would place the last clip 216 frames (~7.2 s)
        # late — the drift this exactness exists to prevent.
        exact = seconds_to_frames(7200.0, Fraction(30000, 1001))
        naive = int(round(7200.0 * 30))
        assert exact == 215784
        assert naive - exact == 216


# ─── MediaInfo rate classification ───────────────────────────────────────────

class TestMediaInfoRates:
    @pytest.mark.parametrize(
        "fps,timebase",
        [
            (Fraction(24000, 1001), 24),
            (Fraction(30000, 1001), 30),
            (Fraction(60000, 1001), 60),
        ],
    )
    def test_broadcast_rates_are_ntsc(self, fps, timebase):
        info = MediaInfo(Path("x"), fps, 1920, 1080, 1.0)
        assert info.is_ntsc is True
        assert info.timebase == timebase

    @pytest.mark.parametrize("fps", [Fraction(24, 1), Fraction(25, 1), Fraction(30, 1), Fraction(60, 1)])
    def test_integer_rates_are_not_ntsc(self, fps):
        assert MediaInfo(Path("x"), fps, 1920, 1080, 1.0).is_ntsc is False


# ─── Timeline structure ──────────────────────────────────────────────────────

class TestTimelineStructure:
    def test_document_shape(self):
        xml = build_fcpxml([_clip(10, 70)], _master())
        assert xml.startswith('<?xml version="1.0" encoding="UTF-8"?>')
        assert "<!DOCTYPE xmeml>" in xml
        root = _parse(xml)
        assert root.tag == "xmeml"
        assert root.get("version") == "4"

    def test_each_moment_gets_linked_video_and_audio(self):
        root = _parse(build_fcpxml([_clip(10, 70), _clip(100, 160)], _master()))
        assert len(root.findall("./sequence/media/video/track/clipitem")) == 2
        assert len(root.findall("./sequence/media/audio/track/clipitem")) == 2

    def test_source_in_out_frames(self):
        root = _parse(build_fcpxml([_clip(100.0, 190.0)], _master()))
        item = root.find("./sequence/media/video/track/clipitem")
        assert item.find("in").text == "3000"
        assert item.find("out").text == "5700"

    def test_clips_are_butted_together_without_gaps(self):
        # Jump-cut assembly: transitions are the editor's job.
        clips = [_clip(100, 190, "a"), _clip(300, 345, "b"), _clip(1000, 1120, "c")]
        root = _parse(build_fcpxml(clips, _master()))
        items = root.findall("./sequence/media/video/track/clipitem")
        starts = [int(i.find("start").text) for i in items]
        ends = [int(i.find("end").text) for i in items]
        assert starts[0] == 0
        assert starts[1] == ends[0]
        assert starts[2] == ends[1]

    def test_sequence_duration_is_sum_of_spans(self):
        clips = [_clip(100, 190), _clip(300, 345)]
        root = _parse(build_fcpxml(clips, _master()))
        assert int(root.find("./sequence/duration").text) == 2700 + 1350

    def test_input_order_does_not_matter(self):
        root = _parse(build_fcpxml([_clip(500, 560, "late"), _clip(10, 70, "early")], _master()))
        names = [i.find("name").text for i in root.findall("./sequence/media/video/track/clipitem")]
        assert names == ["early", "late"]

    def test_master_file_described_once_then_referenced(self):
        root = _parse(build_fcpxml([_clip(10, 70), _clip(100, 160)], _master()))
        files = root.findall(".//file")
        assert len({f.get("id") for f in files}) == 1
        assert sum(1 for f in files if f.find("pathurl") is not None) == 1

    def test_rate_written_for_non_ntsc(self):
        root = _parse(build_fcpxml([_clip(10, 70)], _master()))
        assert root.find("./sequence/rate/timebase").text == "30"
        assert root.find("./sequence/rate/ntsc").text == "FALSE"

    def test_rate_written_for_ntsc(self):
        root = _parse(build_fcpxml([_clip(10, 70)], _master(Fraction(30000, 1001))))
        assert root.find("./sequence/rate/timebase").text == "30"
        assert root.find("./sequence/rate/ntsc").text == "TRUE"

    def test_empty_clip_list_is_valid(self):
        root = _parse(build_fcpxml([], _master()))
        assert root.find("./sequence/duration").text == "0"

    def test_zero_length_clip_is_skipped(self):
        root = _parse(build_fcpxml([_clip(10, 10, "empty")], _master()))
        assert root.findall("./sequence/media/video/track/clipitem") == []


# ─── Metadata + escaping ─────────────────────────────────────────────────────

class TestMetadata:
    def test_special_characters_are_escaped(self):
        xml = build_fcpxml([_clip(10, 70, "B & <moment>")], _master())
        assert "B &amp; &lt;moment&gt;" in xml
        _parse(xml)  # must still parse

    def test_note_carries_score_and_category(self):
        xml = build_fcpxml([_clip(10, 70)], _master())
        assert "score" in xml
        assert "collab_dynamic" in xml

    def test_source_url_is_embedded_for_attribution(self):
        xml = build_fcpxml([_clip(10, 70)], _master(), source_url="https://youtu.be/XYZ")
        assert "https://youtu.be/XYZ" in xml


# ─── Media path linking ──────────────────────────────────────────────────────

class TestPathUrl:
    def test_drive_letter_colon_stays_literal(self):
        # A percent-encoded D%3A cannot be relinked by Premiere.
        url = path_to_url(Path("D:/WORK/out/master.mp4"))
        assert url.startswith("file://localhost/D:/")
        assert "%3A" not in url

    def test_spaces_are_escaped(self):
        url = path_to_url(Path("D:/My Videos/master file.mp4"))
        assert "%20" in url
        assert " " not in url


# ─── Writing to disk ─────────────────────────────────────────────────────────

class TestWriteFcpxml:
    def test_writes_parsable_file(self, tmp_path):
        out = write_fcpxml([_clip(10, 70)], _master(), tmp_path / "sub" / "t.xml")
        assert out.exists()
        ET.parse(out)  # parses from disk, DOCTYPE and all


# ─── Probing real media ──────────────────────────────────────────────────────

class TestProbeMedia:
    def test_missing_file_degrades_with_warning(self, tmp_path):
        logs: list[str] = []
        info = probe_media(tmp_path / "nope.mp4", log_fn=logs.append)
        assert info.fps == Fraction(30, 1)          # documented fallback
        assert any("WARNING" in line for line in logs)
