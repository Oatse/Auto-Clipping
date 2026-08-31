"""Tests for the compilation HTTP surface.

The pipeline itself is covered in test_compilation_pipeline; here we check
the route contract: validation, job lifecycle, and that a finished run
exposes a downloadable timeline.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from web.routes import compilation as comp_route
from web.routes.compilation import router
from web.services import job_state


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(job_state, "COMPILATION_DIR", tmp_path)
    job_state.comp_jobs.clear()
    job_state.comp_tasks.clear()
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as c:
        yield c
    job_state.comp_jobs.clear()


class _Result:
    """Minimal stand-in for CompilationResult."""

    def __init__(self, tmp_path: Path, ok: bool = True, errors=None):
        self.clips = []
        self.master = tmp_path / "master.mp4"
        self.fcpxml = tmp_path / "compilation.xml" if ok else None
        self.manifest = tmp_path / "compilation_manifest.json" if ok else None
        self.errors = errors or []
        self._ok = ok
        if ok:
            self.fcpxml.write_text("<xmeml/>", encoding="utf-8")

    @property
    def ok(self):
        return self._ok

    @property
    def total_seconds(self):
        return 180.0


def _stub_build(monkeypatch, result):
    async def fake_build(**kwargs):
        fake_build.kwargs = kwargs
        log = kwargs.get("log_fn")
        if log:
            log("working")
        return result

    monkeypatch.setattr(comp_route, "build_compilation", fake_build)
    return fake_build


def _wait_done(client, job_id, tries=60):
    for _ in range(tries):
        body = client.get(f"/api/compilation/jobs/{job_id}").json()
        if body["status"] in ("completed", "failed"):
            return body
        import time as _t
        _t.sleep(0.05)
    raise AssertionError("job never finished")


# ─── Validation ──────────────────────────────────────────────────────────────

class TestValidation:
    def test_rejects_empty_url(self, client):
        assert client.post("/api/compilation/jobs", json={"url": "  "}).status_code == 400

    def test_rejects_unknown_model(self, client):
        r = client.post("/api/compilation/jobs", json={"url": "u", "model": "gpt-9"})
        assert r.status_code == 400
        assert "Invalid model" in r.json()["detail"]

    def test_rejects_out_of_range_threshold(self, client):
        r = client.post("/api/compilation/jobs", json={"url": "u", "threshold": 42})
        assert r.status_code == 400

    def test_accepts_claude_backend(self, client, monkeypatch, tmp_path):
        _stub_build(monkeypatch, _Result(tmp_path))
        r = client.post(
            "/api/compilation/jobs",
            json={"url": "u", "model": "claude-opus-4.6"},
        )
        assert r.status_code == 200

    def test_unknown_job_is_404(self, client):
        assert client.get("/api/compilation/jobs/nope").status_code == 404


# ─── Lifecycle ───────────────────────────────────────────────────────────────

class TestLifecycle:
    def test_successful_run_completes_and_reports(self, client, monkeypatch, tmp_path):
        _stub_build(monkeypatch, _Result(tmp_path))
        job_id = client.post(
            "/api/compilation/jobs",
            json={"url": "https://youtu.be/X", "project_name": "P1"},
        ).json()["job_id"]

        body = _wait_done(client, job_id)
        assert body["status"] == "completed"
        assert body["fcpxml_path"]
        assert "min" in body["phase_label"]

    def test_request_fields_reach_the_pipeline(self, client, monkeypatch, tmp_path):
        spy = _stub_build(monkeypatch, _Result(tmp_path))
        job_id = client.post("/api/compilation/jobs", json={
            "url": "https://youtu.be/X", "instructions": "find collabs",
            "lang": "ja", "start_offset": 30.0, "model": "claude-opus-4.6",
            "project_name": "Cut 01", "threshold": 6.5,
            "enable_chat_signals": False,
        }).json()["job_id"]
        _wait_done(client, job_id)

        kw = spy.kwargs
        assert kw["instructions"] == "find collabs"
        assert kw["start_offset"] == 30.0
        assert kw["model"] == "claude-opus-4.6"
        assert kw["threshold"] == 6.5
        assert kw["project_name"] == "Cut 01"
        assert kw["enable_chat"] is False

    def test_failed_run_surfaces_errors(self, client, monkeypatch, tmp_path):
        _stub_build(monkeypatch, _Result(tmp_path, ok=False, errors=["no moments"]))
        job_id = client.post(
            "/api/compilation/jobs", json={"url": "u"},
        ).json()["job_id"]

        body = _wait_done(client, job_id)
        assert body["status"] == "failed"
        assert "no moments" in body["error"]

    def test_crash_is_reported_not_swallowed(self, client, monkeypatch):
        async def boom(**kwargs):
            raise RuntimeError("yt-dlp exploded")

        monkeypatch.setattr(comp_route, "build_compilation", boom)
        job_id = client.post(
            "/api/compilation/jobs", json={"url": "u"},
        ).json()["job_id"]

        body = _wait_done(client, job_id)
        assert body["status"] == "failed"
        assert "yt-dlp exploded" in body["error"]

    def test_jobs_are_listed(self, client, monkeypatch, tmp_path):
        _stub_build(monkeypatch, _Result(tmp_path))
        client.post("/api/compilation/jobs", json={"url": "a"})
        client.post("/api/compilation/jobs", json={"url": "b"})
        assert len(client.get("/api/compilation/jobs").json()["jobs"]) == 2


# ─── Timeline download ───────────────────────────────────────────────────────

class TestSubtitleEndpoint:
    """Subtitling is a polled job, not a held-open request: exporting audio
    and transcribing takes minutes and would time out the HTTP call."""

    def _stub_loop(self, monkeypatch, tmp_path, ok=True, errors=None):
        async def fake_loop(**kwargs):
            fake_loop.kwargs = kwargs
            srt = tmp_path / "timeline.srt"
            srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n", encoding="utf-8")

            class R:
                pass

            r = R()
            r.srt = srt if ok else None
            r.segments = [object(), object()]
            r.speakers = ["SPEAKER_00", "SPEAKER_01"]
            r.speaker_srts = [tmp_path / "timeline.speaker1.srt",
                              tmp_path / "timeline.speaker2.srt"]
            r.graphics = None
            r.imported = ok
            r.errors = errors or []
            r.ok = ok
            return r

        import processors.premiere.subtitle_loop as loop_mod

        monkeypatch.setattr(loop_mod, "subtitle_timeline", fake_loop)
        return fake_loop

    def _wait(self, client, job_id):
        import time as _t

        for _ in range(60):
            body = client.get(f"/api/compilation/subtitle/{job_id}").json()
            if body["status"] != "running":
                return body
            _t.sleep(0.05)
        raise AssertionError("subtitle job never finished")

    def test_returns_a_job_immediately(self, client, monkeypatch, tmp_path):
        self._stub_loop(monkeypatch, tmp_path)
        r = client.post("/api/compilation/subtitle", json={})
        assert r.status_code == 200
        assert r.json()["job_id"]

    def test_completed_job_reports_captions(self, client, monkeypatch, tmp_path):
        self._stub_loop(monkeypatch, tmp_path)
        job_id = client.post("/api/compilation/subtitle", json={}).json()["job_id"]
        body = self._wait(client, job_id)
        assert body["status"] == "completed"
        assert body["segment_count"] == 2
        assert body["speaker_count"] == 2
        assert body["imported"] is True
        assert body["srt_path"].endswith("timeline.srt")

    def test_speaker_options_reach_the_pipeline(self, client, monkeypatch, tmp_path):
        spy = self._stub_loop(monkeypatch, tmp_path)
        job_id = client.post("/api/compilation/subtitle", json={
            "speaker_detection": True, "num_speakers": 4,
        }).json()["job_id"]
        self._wait(client, job_id)
        assert spy.kwargs["num_speakers"] == 4
        assert spy.kwargs["speaker_detection"] is True

    def test_per_speaker_tracks_are_reported(self, client, monkeypatch, tmp_path):
        # SRT cannot show two cues at once, so separate files are what let
        # simultaneous speech keep its own timing on its own track.
        self._stub_loop(monkeypatch, tmp_path)
        job_id = client.post("/api/compilation/subtitle", json={}).json()["job_id"]
        body = self._wait(client, job_id)
        assert len(body["speaker_srt_paths"]) == 2
        assert body["speaker_srt_paths"][0].endswith("timeline.speaker1.srt")

    def test_diarisation_can_be_turned_off(self, client, monkeypatch, tmp_path):
        spy = self._stub_loop(monkeypatch, tmp_path)
        job_id = client.post("/api/compilation/subtitle", json={
            "speaker_detection": False,
        }).json()["job_id"]
        self._wait(client, job_id)
        assert spy.kwargs["speaker_detection"] is False

    def test_translation_defaults_to_english(self, client, monkeypatch, tmp_path):
        spy = self._stub_loop(monkeypatch, tmp_path)
        job_id = client.post("/api/compilation/subtitle", json={}).json()["job_id"]
        self._wait(client, job_id)
        assert spy.kwargs["translate_to"] == "en"

    def test_translation_can_be_disabled(self, client, monkeypatch, tmp_path):
        spy = self._stub_loop(monkeypatch, tmp_path)
        job_id = client.post(
            "/api/compilation/subtitle", json={"translate_to": ""},
        ).json()["job_id"]
        self._wait(client, job_id)
        assert spy.kwargs["translate_to"] is None

    def test_failure_is_reported_on_the_job(self, client, monkeypatch, tmp_path):
        self._stub_loop(monkeypatch, tmp_path, ok=False, errors=["no sequence"])
        job_id = client.post("/api/compilation/subtitle", json={}).json()["job_id"]
        body = self._wait(client, job_id)
        assert body["status"] == "failed"
        assert "no sequence" in body["errors"]

    def test_unknown_subtitle_job_is_404(self, client):
        assert client.get("/api/compilation/subtitle/nope").status_code == 404


class TestTimelineDownload:
    def test_downloads_generated_timeline(self, client, monkeypatch, tmp_path):
        _stub_build(monkeypatch, _Result(tmp_path))
        job_id = client.post(
            "/api/compilation/jobs", json={"url": "u"},
        ).json()["job_id"]
        _wait_done(client, job_id)

        r = client.get(f"/api/compilation/jobs/{job_id}/fcpxml")
        assert r.status_code == 200
        assert "<xmeml" in r.text

    def test_download_before_ready_is_404(self, client, monkeypatch, tmp_path):
        _stub_build(monkeypatch, _Result(tmp_path, ok=False, errors=["boom"]))
        job_id = client.post(
            "/api/compilation/jobs", json={"url": "u"},
        ).json()["job_id"]
        _wait_done(client, job_id)
        assert client.get(f"/api/compilation/jobs/{job_id}/fcpxml").status_code == 404
