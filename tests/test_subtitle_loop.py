"""Tests for the Premiere subtitle loop.

Captions are produced from the *edited* sequence rather than the source VOD,
so the pieces that matter are: finding Premiere's audio preset, exporting the
sequence audio, turning transcript segments into SRT Premiere will accept, and
degrading usefully when a step fails.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from models.transcript import TranscriptSegment
from processors.premiere import subtitle_loop
from processors.premiere.bridge_client import BridgeResponse
from processors.premiere.subtitle_loop import (
    build_srt,
    export_timeline_audio,
    find_audio_preset,
    format_timestamp,
    subtitle_timeline,
    write_srt,
)


def _seg(
    start: float, end: float, text: str, speaker: str = "SPEAKER_00",
) -> TranscriptSegment:
    return TranscriptSegment(start=start, end=end, text=text, speaker=speaker)


class FakeBridge:
    """Records scripts and replays canned responses."""

    def __init__(self, responses=None, available=True):
        self.responses = list(responses or [])
        self.scripts: list[str] = []
        self._available = available

    def available(self) -> bool:
        return self._available

    def unavailable_reason(self) -> str:
        return "connector not running"

    def execute(self, code, *, timeout=None) -> BridgeResponse:
        self.scripts.append(code)
        if self.responses:
            return self.responses.pop(0)
        return BridgeResponse(success=True, data={})


class FakeEngine:
    def __init__(self, segments=None, error=None):
        self.segments = segments if segments is not None else [_seg(0, 2, "hello")]
        self.error = error
        self.calls: list[dict] = []

    async def transcribe(self, audio, output_dir, **kwargs):
        self.calls.append({"audio": Path(audio), **kwargs})
        if self.error:
            raise self.error
        return self.segments, Path(output_dir) / "raw.json"


# ─── SRT ─────────────────────────────────────────────────────────────────────

class TestSrt:
    def test_timestamp_format(self):
        assert format_timestamp(0) == "00:00:00,000"
        assert format_timestamp(1.5) == "00:00:01,500"
        assert format_timestamp(3661.25) == "01:01:01,250"

    def test_negative_time_clamps(self):
        assert format_timestamp(-3) == "00:00:00,000"

    def test_basic_document(self):
        srt = build_srt([_seg(0, 2, "first"), _seg(2.5, 4, "second")])
        assert "1\n00:00:00,000 --> 00:00:02,000\nfirst" in srt
        assert "2\n00:00:02,500 --> 00:00:04,000\nsecond" in srt

    def test_empty_cues_are_dropped_and_renumbered(self):
        # Premiere renders an empty cue as a blank caption rather than
        # skipping it, so they must not reach the file.
        srt = build_srt([_seg(0, 1, "a"), _seg(1, 2, "   "), _seg(2, 3, "b")])
        assert "   " not in srt
        assert srt.count("-->") == 2
        assert "\n2\n" in srt          # renumbered, not 1 then 3

    def test_end_before_start_is_corrected(self):
        srt = build_srt([_seg(5, 3, "reversed")])
        assert "00:00:05,000 --> 00:00:05,000" in srt

    def test_empty_input(self):
        assert build_srt([]) == ""

    def test_write_creates_parent_dirs(self, tmp_path):
        out = write_srt([_seg(0, 1, "x")], tmp_path / "deep" / "t.srt")
        assert out.is_file()
        assert "x" in out.read_text(encoding="utf-8")


# ─── Speaker labelling ───────────────────────────────────────────────────────

class TestSpeakerLabels:
    def test_solo_stream_is_not_labelled(self):
        # Prefixing every line of a one-person clip with "Speaker 1" is pure
        # noise, so the decision comes from the data, not the flag.
        srt = build_srt([_seg(0, 1, "hello"), _seg(1, 2, "again")])
        assert "Speaker" not in srt

    def test_multiple_speakers_are_labelled(self):
        srt = build_srt([
            _seg(0, 1, "hello", "SPEAKER_00"),
            _seg(1, 2, "hi there", "SPEAKER_01"),
        ])
        assert "Speaker 1: hello" in srt
        assert "Speaker 2: hi there" in srt

    def test_label_only_repeats_on_change(self):
        # Repeating the name on every cue of one turn wastes caption width.
        srt = build_srt([
            _seg(0, 1, "one", "SPEAKER_00"),
            _seg(1, 2, "two", "SPEAKER_00"),
            _seg(2, 3, "three", "SPEAKER_01"),
        ])
        assert "Speaker 1: one" in srt
        assert "\ntwo\n" in srt              # same speaker, no repeat
        assert "Speaker 2: three" in srt

    def test_numbering_follows_first_appearance(self):
        # Not the diarisation id: SPEAKER_03 speaking first is "Speaker 1".
        srt = build_srt([
            _seg(0, 1, "first", "SPEAKER_03"),
            _seg(1, 2, "second", "SPEAKER_01"),
        ])
        assert "Speaker 1: first" in srt
        assert "Speaker 2: second" in srt

    def test_labels_can_be_disabled(self):
        srt = build_srt(
            [_seg(0, 1, "a", "SPEAKER_00"), _seg(1, 2, "b", "SPEAKER_01")],
            speaker_labels=False,
        )
        assert "Speaker" not in srt

    def test_speaker_returning_reuses_its_number(self):
        srt = build_srt([
            _seg(0, 1, "a", "SPEAKER_00"),
            _seg(1, 2, "b", "SPEAKER_01"),
            _seg(2, 3, "c", "SPEAKER_00"),
        ])
        assert srt.count("Speaker 1:") == 2
        assert srt.count("Speaker 2:") == 1


class TestSpeakerReporting:
    def _run(self, **kwargs):
        return asyncio.run(subtitle_timeline(**kwargs))

    def _patch_export(self, monkeypatch):
        monkeypatch.setattr(
            subtitle_loop, "export_timeline_audio",
            lambda b, out, **k: (Path(out).write_bytes(b"RIFF"),
                                 BridgeResponse(success=True))[1],
        )

    def test_detected_speakers_are_reported(self, tmp_path, monkeypatch):
        self._patch_export(monkeypatch)
        result = self._run(
            output_dir=tmp_path, bridge=FakeBridge(),
            engine=FakeEngine([
                _seg(0, 1, "a", "SPEAKER_00"),
                _seg(1, 2, "b", "SPEAKER_01"),
            ]),
        )
        assert result.speakers == ["SPEAKER_00", "SPEAKER_01"]

    def test_speaker_hint_reaches_the_engine(self, tmp_path, monkeypatch):
        self._patch_export(monkeypatch)
        engine = FakeEngine()
        self._run(
            output_dir=tmp_path, bridge=FakeBridge(), engine=engine,
            num_speakers=4, speaker_detection=True,
        )
        assert engine.calls[0]["num_speakers"] == 4
        assert engine.calls[0]["speaker_detection"] is True

    def test_warns_when_collab_collapses_to_one_speaker(self, tmp_path, monkeypatch):
        # The confusing outcome is unlabelled captions for a multi-person
        # clip, so say the hint exists rather than staying quiet.
        self._patch_export(monkeypatch)
        logs: list[str] = []
        self._run(
            output_dir=tmp_path, bridge=FakeBridge(),
            engine=FakeEngine([_seg(0, 1, "a", "SPEAKER_00")]),
            log_fn=logs.append,
        )
        assert any("speaker count" in line for line in logs)

    def test_no_warning_when_count_was_given(self, tmp_path, monkeypatch):
        self._patch_export(monkeypatch)
        logs: list[str] = []
        self._run(
            output_dir=tmp_path, bridge=FakeBridge(),
            engine=FakeEngine([_seg(0, 1, "a", "SPEAKER_00")]),
            num_speakers=1, log_fn=logs.append,
        )
        assert not any("speaker count" in line for line in logs)


# ─── Preset discovery ────────────────────────────────────────────────────────

class TestPresetDiscovery:
    def test_explicit_path_wins(self, tmp_path):
        preset = tmp_path / "custom.epr"
        preset.write_text("x")
        assert find_audio_preset(str(preset)) == preset

    def test_env_var_is_honoured(self, tmp_path, monkeypatch):
        preset = tmp_path / "env.epr"
        preset.write_text("x")
        monkeypatch.setenv("PREMIERE_AUDIO_PRESET", str(preset))
        assert find_audio_preset() == preset

    def test_missing_explicit_falls_through(self, tmp_path, monkeypatch):
        monkeypatch.delenv("PREMIERE_AUDIO_PRESET", raising=False)
        monkeypatch.setattr(subtitle_loop, "_PRESET_ROOTS", (str(tmp_path),))
        assert find_audio_preset(str(tmp_path / "nope.epr")) is None

    def test_finds_preset_under_systempresets(self, tmp_path, monkeypatch):
        monkeypatch.delenv("PREMIERE_AUDIO_PRESET", raising=False)
        nested = tmp_path / "Premiere 2099" / "MediaIO" / "systempresets" / "ABC"
        nested.mkdir(parents=True)
        preset = nested / subtitle_loop._PRESET_NAME
        preset.write_text("x")
        monkeypatch.setattr(subtitle_loop, "_PRESET_ROOTS", (str(tmp_path),))
        assert find_audio_preset() == preset


# ─── Audio export ────────────────────────────────────────────────────────────

class TestExportAudio:
    def test_reports_missing_preset(self, tmp_path, monkeypatch):
        monkeypatch.setattr(subtitle_loop, "find_audio_preset", lambda *a, **k: None)
        result = export_timeline_audio(FakeBridge(), tmp_path / "a.wav")
        assert result.success is False
        assert "PREMIERE_AUDIO_PRESET" in result.error

    def test_script_uses_export_as_media_direct(self, tmp_path):
        preset = tmp_path / "p.epr"
        preset.write_text("x")
        bridge = FakeBridge()
        export_timeline_audio(bridge, tmp_path / "a.wav", preset=preset)

        script = bridge.scripts[0]
        assert "exportAsMediaDirect" in script
        assert "activeSequence" in script
        # exportAsMediaDirect reports failure by RETURNING a message rather
        # than throwing, so the script must check for "No Error".
        assert "No Error" in script

    def test_output_path_is_absolute(self, tmp_path):
        preset = tmp_path / "p.epr"
        preset.write_text("x")
        bridge = FakeBridge()
        export_timeline_audio(bridge, Path("relative.wav"), preset=preset)
        assert str(Path("relative.wav").resolve()).replace("\\", "/") in \
            bridge.scripts[0].replace("\\\\", "/")


# ─── The whole loop ──────────────────────────────────────────────────────────

class TestSubtitleTimeline:
    def _run(self, **kwargs):
        return asyncio.run(subtitle_timeline(**kwargs))

    def test_happy_path(self, tmp_path, monkeypatch):
        preset = tmp_path / "p.epr"
        preset.write_text("x")
        monkeypatch.setattr(subtitle_loop, "find_audio_preset", lambda *a, **k: preset)

        audio = tmp_path / "timeline_audio.wav"

        def fake_export(bridge, out_path, **kwargs):
            Path(out_path).write_bytes(b"RIFF")
            return BridgeResponse(success=True, data={"exported": True})

        monkeypatch.setattr(subtitle_loop, "export_timeline_audio", fake_export)
        engine = FakeEngine([_seg(0, 2, "hello"), _seg(2, 4, "world")])

        result = self._run(
            output_dir=tmp_path, bridge=FakeBridge(), engine=engine,
        )

        assert result.ok
        assert result.srt and result.srt.is_file()
        assert len(result.segments) == 2
        assert result.imported is True
        assert "hello" in result.srt.read_text(encoding="utf-8")

    def test_audio_is_deleted_by_default(self, tmp_path, monkeypatch):
        # ~150 MB per run would quietly fill the disk.
        monkeypatch.setattr(
            subtitle_loop, "export_timeline_audio",
            lambda b, out, **k: (Path(out).write_bytes(b"RIFF"),
                                 BridgeResponse(success=True))[1],
        )
        result = self._run(
            output_dir=tmp_path, bridge=FakeBridge(), engine=FakeEngine(),
        )
        assert not (tmp_path / "timeline_audio.wav").exists()
        assert result.audio is None

    def test_audio_kept_when_asked(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            subtitle_loop, "export_timeline_audio",
            lambda b, out, **k: (Path(out).write_bytes(b"RIFF"),
                                 BridgeResponse(success=True))[1],
        )
        result = self._run(
            output_dir=tmp_path, bridge=FakeBridge(), engine=FakeEngine(),
            keep_audio=True,
        )
        assert result.audio and result.audio.is_file()

    def test_offline_bridge_is_reported(self, tmp_path):
        result = self._run(
            output_dir=tmp_path, bridge=FakeBridge(available=False),
            engine=FakeEngine(),
        )
        assert not result.ok
        assert "connector" in result.errors[0]

    def test_export_failure_is_reported(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            subtitle_loop, "export_timeline_audio",
            lambda b, out, **k: BridgeResponse.failed("no sequence"),
        )
        result = self._run(
            output_dir=tmp_path, bridge=FakeBridge(), engine=FakeEngine(),
        )
        assert not result.ok
        assert "no sequence" in result.errors[0]

    def test_silent_export_failure_is_caught(self, tmp_path, monkeypatch):
        # Premiere claiming success without writing a file must not be
        # mistaken for a transcribable audio track.
        monkeypatch.setattr(
            subtitle_loop, "export_timeline_audio",
            lambda b, out, **k: BridgeResponse(success=True),
        )
        result = self._run(
            output_dir=tmp_path, bridge=FakeBridge(), engine=FakeEngine(),
        )
        assert not result.ok
        assert "no audio file" in result.errors[0]

    def test_transcription_failure_is_reported(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            subtitle_loop, "export_timeline_audio",
            lambda b, out, **k: (Path(out).write_bytes(b"RIFF"),
                                 BridgeResponse(success=True))[1],
        )
        result = self._run(
            output_dir=tmp_path, bridge=FakeBridge(),
            engine=FakeEngine(error=RuntimeError("quota exceeded")),
        )
        assert not result.ok
        assert "quota exceeded" in result.errors[0]

    def test_failed_import_still_returns_the_srt(self, tmp_path, monkeypatch):
        # The captions exist and can be imported by hand; losing them because
        # the last step failed would be the wrong trade.
        monkeypatch.setattr(
            subtitle_loop, "export_timeline_audio",
            lambda b, out, **k: (Path(out).write_bytes(b"RIFF"),
                                 BridgeResponse(success=True))[1],
        )
        monkeypatch.setattr(
            subtitle_loop, "import_captions",
            lambda b, srt, **k: BridgeResponse.failed("refused"),
        )
        result = self._run(
            output_dir=tmp_path, bridge=FakeBridge(), engine=FakeEngine(),
        )
        assert result.srt and result.srt.is_file()
        assert result.imported is False
        assert any("refused" in e for e in result.errors)

    def test_translation_replaces_text_and_keeps_timing(self, tmp_path, monkeypatch):
        # A JP stream captioned for an EN audience is the normal case, and the
        # timings must survive since they came from the exported timeline.
        monkeypatch.setattr(
            subtitle_loop, "export_timeline_audio",
            lambda b, out, **k: (Path(out).write_bytes(b"RIFF"),
                                 BridgeResponse(success=True))[1],
        )

        class FakeTranslator:
            async def translate(self, segments, output_dir, regroup=False):
                out = [
                    TranscriptSegment(
                        start=s.start, end=s.end,
                        text="EN:" + s.text, speaker=s.speaker,
                    )
                    for s in segments
                ]
                return out, Path(output_dir) / "translated.json"

        result = self._run(
            output_dir=tmp_path, bridge=FakeBridge(),
            engine=FakeEngine([_seg(1.5, 3.0, "こんにちは")]),
            translate_to="en", translator=FakeTranslator(),
        )

        assert result.ok
        srt = result.srt.read_text(encoding="utf-8")
        assert "EN:こんにちは" in srt
        assert "00:00:01,500 --> 00:00:03,000" in srt

    def test_translation_failure_keeps_source_captions(self, tmp_path, monkeypatch):
        # The transcription is already paid for; losing it because the
        # translator failed would be the wrong trade.
        monkeypatch.setattr(
            subtitle_loop, "export_timeline_audio",
            lambda b, out, **k: (Path(out).write_bytes(b"RIFF"),
                                 BridgeResponse(success=True))[1],
        )

        class BoomTranslator:
            async def translate(self, segments, output_dir, regroup=False):
                raise RuntimeError("backend down")

        result = self._run(
            output_dir=tmp_path, bridge=FakeBridge(),
            engine=FakeEngine([_seg(0, 1, "もしもし")]),
            translate_to="en", translator=BoomTranslator(),
        )
        assert result.srt and "もしもし" in result.srt.read_text(encoding="utf-8")
        assert any("translation failed" in e for e in result.errors)

    def test_import_can_be_skipped(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            subtitle_loop, "export_timeline_audio",
            lambda b, out, **k: (Path(out).write_bytes(b"RIFF"),
                                 BridgeResponse(success=True))[1],
        )
        called = []
        monkeypatch.setattr(
            subtitle_loop, "import_captions",
            lambda *a, **k: called.append(1),
        )
        result = self._run(
            output_dir=tmp_path, bridge=FakeBridge(), engine=FakeEngine(),
            import_back=False,
        )
        assert result.ok
        assert called == []
