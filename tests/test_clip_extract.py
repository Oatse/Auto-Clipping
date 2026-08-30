"""Tests for per-moment clip extraction and the timeline built over it.

Referencing an 80-minute long-GOP master at scattered points is what makes a
Premiere timeline stall: at 60 fps with a keyframe every ~6 s, showing one
frame can mean decoding hundreds. Cutting each moment into its own short file
fixes that, and handles keep the in/out points adjustable afterwards.
"""

from __future__ import annotations

import asyncio
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path

import pytest

from models.clip import Clip, ClipScore
from processors.premiere import clip_extract
from processors.premiere.clip_extract import (
    KEYFRAME_INTERVAL_SECONDS,
    ExtractedClip,
    _encode_args,
    extract_moments,
)
from processors.premiere.fcpxml import build_fcpxml_from_extracted
from processors.premiere.source import MediaInfo


def _clip(start: float, end: float, title: str = "m") -> Clip:
    return Clip(start=start, end=end, title=title, score=ClipScore(),
                score_profile="vtuber")


def _master(fps: Fraction = Fraction(60, 1)) -> MediaInfo:
    return MediaInfo(
        path=Path("D:/out/master.mp4"), fps=fps,
        width=1920, height=1080, duration=4806.0,
    )


def _fake_ffmpeg(monkeypatch, *, succeed=True, record=None):
    async def fake_run(args):
        if record is not None:
            record.append(args)
        if succeed:
            # ffmpeg's output path is the last argument.
            Path(args[-1]).write_bytes(b"fake-media")
        return succeed

    monkeypatch.setattr(clip_extract, "_run", fake_run)
    monkeypatch.setattr(clip_extract, "has_nvenc", lambda *a, **k: False)


# ─── Encoder settings ────────────────────────────────────────────────────────

class TestEncodeArgs:
    def test_keyframe_every_second(self):
        # The point of re-encoding is scrubbing; a long GOP would hand back
        # exactly the problem being solved.
        args = _encode_args(60.0, use_nvenc=False)
        assert args[args.index("-g") + 1] == str(int(60 * KEYFRAME_INTERVAL_SECONDS))

    def test_b_frames_disabled(self):
        # B-frames make seeking more expensive for no benefit here.
        for use_nvenc in (True, False):
            args = _encode_args(30.0, use_nvenc=use_nvenc)
            assert args[args.index("-bf") + 1] == "0"

    def test_nvenc_used_when_available(self):
        assert "h264_nvenc" in _encode_args(30.0, use_nvenc=True)
        assert "libx264" in _encode_args(30.0, use_nvenc=False)

    def test_audio_is_kept(self):
        for use_nvenc in (True, False):
            assert "aac" in _encode_args(30.0, use_nvenc=use_nvenc)


# ─── Extraction ──────────────────────────────────────────────────────────────

class TestExtractMoments:
    def _run(self, **kwargs):
        return asyncio.run(extract_moments(**kwargs))

    def test_one_file_per_moment(self, tmp_path, monkeypatch):
        _fake_ffmpeg(monkeypatch)
        out = self._run(
            master=tmp_path / "master.mp4",
            clips=[_clip(100, 190), _clip(300, 360)],
            output_dir=tmp_path / "clips",
            fps=60.0,
        )
        assert len(out) == 2
        assert all(item.path.is_file() for item in out)
        assert [p.path.name for p in out] == ["moment_001.mp4", "moment_002.mp4"]

    def test_handles_pad_both_sides(self, tmp_path, monkeypatch):
        record: list = []
        _fake_ffmpeg(monkeypatch, record=record)
        out = self._run(
            master=tmp_path / "master.mp4", clips=[_clip(100, 190)],
            output_dir=tmp_path / "clips", fps=60.0, handle_seconds=15.0,
        )
        args = record[0]
        assert args[args.index("-ss") + 1] == "85.000"        # 100 - 15
        assert args[args.index("-t") + 1] == "120.000"        # 90 + 2*15
        assert out[0].handle_start == pytest.approx(15.0)
        assert out[0].moment_out == pytest.approx(105.0)      # 15 + 90

    def test_handle_clamped_at_start_of_master(self, tmp_path, monkeypatch):
        record: list = []
        _fake_ffmpeg(monkeypatch, record=record)
        out = self._run(
            master=tmp_path / "master.mp4", clips=[_clip(5, 30)],
            output_dir=tmp_path / "clips", fps=60.0, handle_seconds=15.0,
        )
        assert record[0][record[0].index("-ss") + 1] == "0.000"
        assert out[0].handle_start == pytest.approx(5.0)      # only what exists

    def test_handle_clamped_at_end_of_master(self, tmp_path, monkeypatch):
        record: list = []
        _fake_ffmpeg(monkeypatch, record=record)
        self._run(
            master=tmp_path / "master.mp4", clips=[_clip(4700, 4790)],
            output_dir=tmp_path / "clips", fps=60.0, handle_seconds=15.0,
            master_duration=4800.0,
        )
        args = record[0]
        # 4685 -> 4800, not 4805
        assert args[args.index("-t") + 1] == "115.000"

    def test_stream_copy_mode(self, tmp_path, monkeypatch):
        record: list = []
        _fake_ffmpeg(monkeypatch, record=record)
        self._run(
            master=tmp_path / "master.mp4", clips=[_clip(100, 190)],
            output_dir=tmp_path / "clips", fps=60.0, reencode=False,
        )
        assert "copy" in record[0]
        assert "libx264" not in record[0]

    def test_failed_clip_is_skipped_not_fatal(self, tmp_path, monkeypatch):
        _fake_ffmpeg(monkeypatch, succeed=False)
        out = self._run(
            master=tmp_path / "master.mp4", clips=[_clip(100, 190)],
            output_dir=tmp_path / "clips", fps=60.0,
        )
        assert out == []

    def test_zero_length_moment_is_skipped(self, tmp_path, monkeypatch):
        _fake_ffmpeg(monkeypatch)
        out = self._run(
            master=tmp_path / "master.mp4", clips=[_clip(100, 100)],
            output_dir=tmp_path / "clips", fps=60.0, handle_seconds=0.0,
        )
        assert out == []


# ─── Timeline over extracted clips ───────────────────────────────────────────

class TestTimelineFromExtracted:
    def _extracted(self, tmp_path, count=2):
        items = []
        for i in range(count):
            path = tmp_path / f"moment_{i + 1:03d}.mp4"
            path.write_bytes(b"x")
            items.append(
                ExtractedClip(
                    clip=_clip(100 + i * 300, 190 + i * 300, f"m{i + 1}"),
                    path=path, handle_start=15.0, duration=120.0,
                )
            )
        return items

    def _parse(self, xml: str) -> ET.Element:
        return ET.fromstring(xml[xml.index("<xmeml"):])

    def test_references_each_clip_file(self, tmp_path):
        items = self._extracted(tmp_path)
        root = self._parse(build_fcpxml_from_extracted(items, _master()))
        urls = [f.text for f in root.findall(".//pathurl")]
        assert len(urls) == 2
        assert "moment_001.mp4" in urls[0]
        assert "moment_002.mp4" in urls[1]
        assert not any("master.mp4" in u for u in urls)

    def test_in_out_offset_by_the_handle(self, tmp_path):
        # The moment starts 15 s into its file, not at 0.
        items = self._extracted(tmp_path, count=1)
        root = self._parse(build_fcpxml_from_extracted(items, _master()))
        item = root.find("./sequence/media/video/track/clipitem")
        assert item.find("in").text == str(15 * 60)      # 15 s at 60 fps
        assert item.find("out").text == str((15 + 90) * 60)

    def test_clips_are_butted_together(self, tmp_path):
        items = self._extracted(tmp_path)
        root = self._parse(build_fcpxml_from_extracted(items, _master()))
        vid = root.findall("./sequence/media/video/track/clipitem")
        assert int(vid[0].find("start").text) == 0
        assert int(vid[1].find("start").text) == int(vid[0].find("end").text)

    def test_each_moment_has_linked_audio(self, tmp_path):
        items = self._extracted(tmp_path)
        root = self._parse(build_fcpxml_from_extracted(items, _master()))
        assert len(root.findall("./sequence/media/video/track/clipitem")) == 2
        assert len(root.findall("./sequence/media/audio/track/clipitem")) == 2

    def test_each_file_is_described_once(self, tmp_path):
        # Unlike the single-master timeline, every moment is a different file,
        # so each needs its own definition — but only once.
        items = self._extracted(tmp_path)
        root = self._parse(build_fcpxml_from_extracted(items, _master()))
        ids = [f.get("id") for f in root.findall(".//file")]
        assert ids.count("file-moment-1") == 2      # video defines, audio refs
        described = [
            f.get("id") for f in root.findall(".//file")
            if f.find("pathurl") is not None
        ]
        assert sorted(described) == ["file-moment-1", "file-moment-2"]

    def test_sequence_duration_excludes_handles(self, tmp_path):
        # Handles exist for trimming; they are not part of the cut.
        items = self._extracted(tmp_path)
        root = self._parse(build_fcpxml_from_extracted(items, _master()))
        assert int(root.find("./sequence/duration").text) == 2 * 90 * 60

    def test_empty_input(self, tmp_path):
        root = self._parse(build_fcpxml_from_extracted([], _master()))
        assert root.find("./sequence/duration").text == "0"
