"""Tests for the compilation pipeline (URL -> Premiere timeline).

Exercised with a fake ClipFinder and a patched MasterSource so the whole
flow runs without network or ffmpeg. What matters here is the wiring:

  * analysis and the master download actually overlap (the reason the
    pipeline exists in this shape);
  * COMPILATION mode is what reaches find_clips;
  * partial failure still returns what succeeded instead of raising.
"""

from __future__ import annotations

import asyncio
import json
from fractions import Fraction
from pathlib import Path

import pytest

from models.clip import Clip, ClipScore, HighlightType
from processors.premiere import pipeline as pipeline_mod
from processors.premiere.pipeline import build_compilation
from processors.premiere.source import MediaInfo


def _clip(start: float, end: float, title: str = "m") -> Clip:
    return Clip(
        start=start, end=end, title=title, reason="because",
        highlight_type=HighlightType.GENUINE_REACTION,
        score=ClipScore(quotability=9.0), score_profile="vtuber",
    )


class FakeFinder:
    """Stands in for ClipFinder, recording how it was called."""

    def __init__(self, clips=None, transcript=None, delay: float = 0.0):
        self._clips = clips if clips is not None else [_clip(10, 100), _clip(200, 290)]
        self._transcript = (
            transcript if transcript is not None
            else [{"start": 0.0, "end": 5.0, "text": "hi"}]
        )
        self._delay = delay
        self.find_kwargs: dict = {}
        self.analysis_window: tuple[float, float] | None = None

    async def extract_subtitles(self, url, output_dir, lang="ja", log_fn=None, **kw):
        self._t0 = asyncio.get_event_loop().time()
        if self._delay:
            await asyncio.sleep(self._delay)
        return self._transcript

    async def extract_signals(self, url, output_dir, **kw):
        return []

    @staticmethod
    def filter_transcript_by_offset(transcript, offset):
        return [s for s in transcript if s["end"] > offset]

    async def find_clips(self, **kwargs):
        self.find_kwargs = kwargs
        self.analysis_window = (self._t0, asyncio.get_event_loop().time())
        return self._clips


def _patch_master(monkeypatch, tmp_path, *, delay: float = 0.0, fail: bool = False):
    """Replace MasterSource.download_master with a local fake file."""
    window: dict = {}

    async def fake_download_master(self, *, url, output_dir, log_fn=None, **kw):
        window["start"] = asyncio.get_event_loop().time()
        if delay:
            await asyncio.sleep(delay)
        window["end"] = asyncio.get_event_loop().time()
        if fail:
            return None
        path = Path(output_dir) / "master.mp4"
        path.write_bytes(b"not-real-video")
        return path

    monkeypatch.setattr(
        pipeline_mod.MasterSource, "download_master", fake_download_master,
    )
    monkeypatch.setattr(
        pipeline_mod, "probe_media",
        lambda path, **kw: MediaInfo(Path(path), Fraction(30, 1), 1920, 1080, 3600.0),
    )
    return window


# ─── Happy path ──────────────────────────────────────────────────────────────

class TestBuildCompilation:
    def test_produces_timeline_master_and_manifest(self, tmp_path, monkeypatch):
        _patch_master(monkeypatch, tmp_path)
        finder = FakeFinder()

        result = asyncio.run(build_compilation(
            url="https://youtu.be/X", output_dir=tmp_path, api_keys=["k"],
            finder=finder, project_name="My Comp",
        ))

        assert result.ok
        assert result.master and result.master.exists()
        assert result.fcpxml and result.fcpxml.exists()
        assert result.manifest and result.manifest.exists()
        assert len(result.clips) == 2
        assert result.errors == []

    def test_requests_compilation_mode(self, tmp_path, monkeypatch):
        _patch_master(monkeypatch, tmp_path)
        finder = FakeFinder()

        asyncio.run(build_compilation(
            url="u", output_dir=tmp_path, api_keys=["k"], finder=finder,
            threshold=6.5, model="claude-opus-4.6",
        ))

        assert finder.find_kwargs["clip_format"] == "compilation"
        assert finder.find_kwargs["threshold"] == 6.5
        assert finder.find_kwargs["model"] == "claude-opus-4.6"

    def test_manifest_records_source_for_attribution(self, tmp_path, monkeypatch):
        _patch_master(monkeypatch, tmp_path)
        result = asyncio.run(build_compilation(
            url="https://youtu.be/ABC", output_dir=tmp_path, api_keys=["k"],
            finder=FakeFinder(), project_name="Attrib",
        ))
        data = json.loads(result.manifest.read_text(encoding="utf-8"))
        assert data["source_url"] == "https://youtu.be/ABC"
        assert data["project"] == "Attrib"
        assert data["moment_count"] == 2
        assert data["fps"] == 30.0
        assert len(data["moments"]) == 2

    def test_timeline_carries_project_name(self, tmp_path, monkeypatch):
        _patch_master(monkeypatch, tmp_path)
        result = asyncio.run(build_compilation(
            url="u", output_dir=tmp_path, api_keys=["k"],
            finder=FakeFinder(), project_name="Hololive Cut 01",
        ))
        assert "Hololive Cut 01" in result.fcpxml.read_text(encoding="utf-8")

    def test_total_seconds_reports_material_length(self, tmp_path, monkeypatch):
        _patch_master(monkeypatch, tmp_path)
        result = asyncio.run(build_compilation(
            url="u", output_dir=tmp_path, api_keys=["k"], finder=FakeFinder(),
        ))
        assert result.total_seconds == pytest.approx(180.0)


# ─── Concurrency ─────────────────────────────────────────────────────────────

class TestClipExtraction:
    """Cutting moments out of the master is what makes the timeline editable;
    referencing an 80-minute long-GOP file is why scrubbing stalls."""

    def _patch_extract(self, monkeypatch, tmp_path, *, succeed_count=None):
        calls: dict = {}

        async def fake_extract(*, master, clips, output_dir, **kwargs):
            calls["clips"] = list(clips)
            calls["kwargs"] = kwargs
            from processors.premiere.clip_extract import ExtractedClip

            Path(output_dir).mkdir(parents=True, exist_ok=True)
            made = []
            wanted = len(clips) if succeed_count is None else succeed_count
            for i, clip in enumerate(clips[:wanted], start=1):
                path = Path(output_dir) / f"moment_{i:03d}.mp4"
                path.write_bytes(b"x")
                made.append(
                    ExtractedClip(clip=clip, path=path, handle_start=15.0,
                                  duration=clip.duration + 30.0)
                )
            return made

        import processors.premiere.clip_extract as ce

        monkeypatch.setattr(ce, "extract_moments", fake_extract)
        return calls

    def test_timeline_references_the_extracted_clips(self, tmp_path, monkeypatch):
        _patch_master(monkeypatch, tmp_path)
        self._patch_extract(monkeypatch, tmp_path)

        result = asyncio.run(build_compilation(
            url="u", output_dir=tmp_path, api_keys=["k"], finder=FakeFinder(),
        ))

        assert len(result.clip_files) == 2
        xml = result.fcpxml.read_text(encoding="utf-8")
        assert "moment_001.mp4" in xml
        assert "master.mp4" not in xml

    def test_handles_are_passed_through(self, tmp_path, monkeypatch):
        _patch_master(monkeypatch, tmp_path)
        calls = self._patch_extract(monkeypatch, tmp_path)

        asyncio.run(build_compilation(
            url="u", output_dir=tmp_path, api_keys=["k"], finder=FakeFinder(),
            handle_seconds=30.0, clip_quality=25,
        ))
        assert calls["kwargs"]["handle_seconds"] == 30.0
        assert calls["kwargs"]["quality"] == 25

    def test_extraction_can_be_disabled(self, tmp_path, monkeypatch):
        _patch_master(monkeypatch, tmp_path)
        calls = self._patch_extract(monkeypatch, tmp_path)

        result = asyncio.run(build_compilation(
            url="u", output_dir=tmp_path, api_keys=["k"], finder=FakeFinder(),
            extract_clips=False,
        ))
        assert calls == {}                       # never called
        assert result.clip_files == []
        assert "master.mp4" in result.fcpxml.read_text(encoding="utf-8")

    def test_partial_failure_falls_back_wholesale(self, tmp_path, monkeypatch):
        # A half-extracted timeline would reference two kinds of media with
        # different in/out semantics; falling back keeps it consistent.
        _patch_master(monkeypatch, tmp_path)
        self._patch_extract(monkeypatch, tmp_path, succeed_count=1)

        result = asyncio.run(build_compilation(
            url="u", output_dir=tmp_path, api_keys=["k"], finder=FakeFinder(),
        ))
        xml = result.fcpxml.read_text(encoding="utf-8")
        assert "master.mp4" in xml
        assert "moment_001.mp4" not in xml


class TestParallelism:
    def test_download_and_analysis_overlap(self, tmp_path, monkeypatch):
        # The whole point of the pipeline's shape: a long master download
        # must not block analysis, which only needs subtitles and chat.
        window = _patch_master(monkeypatch, tmp_path, delay=0.15)
        finder = FakeFinder(delay=0.15)

        asyncio.run(build_compilation(
            url="u", output_dir=tmp_path, api_keys=["k"], finder=finder,
        ))

        dl_start, dl_end = window["start"], window["end"]
        an_start, an_end = finder.analysis_window
        overlap = min(dl_end, an_end) - max(dl_start, an_start)
        assert overlap > 0, "download and analysis ran sequentially"


# ─── Failure handling ────────────────────────────────────────────────────────

class TestFailureHandling:
    def test_master_failure_keeps_moments_and_reports(self, tmp_path, monkeypatch):
        _patch_master(monkeypatch, tmp_path, fail=True)
        result = asyncio.run(build_compilation(
            url="u", output_dir=tmp_path, api_keys=["k"], finder=FakeFinder(),
        ))
        assert not result.ok
        assert len(result.clips) == 2          # analysis survived
        assert result.fcpxml is None
        assert any("master" in e for e in result.errors)

    def test_no_moments_reports_without_raising(self, tmp_path, monkeypatch):
        _patch_master(monkeypatch, tmp_path)
        result = asyncio.run(build_compilation(
            url="u", output_dir=tmp_path, api_keys=["k"],
            finder=FakeFinder(clips=[]),
        ))
        assert not result.ok
        assert any("quality bar" in e for e in result.errors)

    def test_missing_subtitles_reported_not_raised(self, tmp_path, monkeypatch):
        _patch_master(monkeypatch, tmp_path)
        result = asyncio.run(build_compilation(
            url="u", output_dir=tmp_path, api_keys=["k"],
            finder=FakeFinder(transcript=[]),
        ))
        assert not result.ok
        assert any("subtitle" in e.lower() for e in result.errors)

    def test_start_offset_filters_transcript(self, tmp_path, monkeypatch):
        _patch_master(monkeypatch, tmp_path)
        finder = FakeFinder(transcript=[
            {"start": 0.0, "end": 5.0, "text": "waiting"},
            {"start": 100.0, "end": 105.0, "text": "live"},
        ])
        asyncio.run(build_compilation(
            url="u", output_dir=tmp_path, api_keys=["k"],
            finder=finder, start_offset=50.0,
        ))
        assert finder.find_kwargs["transcript"] == [
            {"start": 100.0, "end": 105.0, "text": "live"}
        ]

    def test_offset_consuming_everything_is_reported(self, tmp_path, monkeypatch):
        _patch_master(monkeypatch, tmp_path)
        result = asyncio.run(build_compilation(
            url="u", output_dir=tmp_path, api_keys=["k"],
            finder=FakeFinder(transcript=[{"start": 0.0, "end": 5.0, "text": "x"}]),
            start_offset=999.0,
        ))
        assert not result.ok
        assert any("start offset" in e for e in result.errors)
