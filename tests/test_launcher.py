"""Tests for the launcher's guards, bridge probe, and orchestration.

The property that matters most is idempotence: running the launcher when
everything is already up must start nothing. Nothing here spawns a real
process — ``_spawn`` and the probes are patched.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from launcher import bridge as bridge_mod
from launcher import components as comp
from launcher import orchestrator as orch
from launcher.bridge import (
    HEARTBEAT_NAME,
    BridgeConfig,
    bridge_alive,
    bridge_state,
    ensure_bridge,
)
from launcher.components import (
    ComponentStatus,
    State,
    ensure_http_service,
    ensure_process,
    wait_until,
)
from launcher.orchestrator import LaunchOptions, launch
from launcher.singleton import AlreadyRunning, single_instance


# ─── wait_until ──────────────────────────────────────────────────────────────

class TestWaitUntil:
    def test_returns_immediately_when_already_true(self):
        started = time.monotonic()
        assert wait_until(lambda: True, timeout=5.0) is True
        assert time.monotonic() - started < 0.2

    def test_returns_false_after_timeout(self):
        assert wait_until(lambda: False, timeout=0.2, initial_interval=0.05) is False

    def test_succeeds_once_predicate_flips(self):
        calls = {"n": 0}

        def predicate() -> bool:
            calls["n"] += 1
            return calls["n"] >= 3

        assert wait_until(predicate, timeout=5.0, initial_interval=0.01) is True


# ─── Idempotent guards ───────────────────────────────────────────────────────

class TestEnsureHttpService:
    def test_adopts_running_service_without_spawning(self, monkeypatch):
        spawned: list = []
        monkeypatch.setattr(comp, "http_alive", lambda url, timeout=2.0: True)
        monkeypatch.setattr(comp, "_spawn", lambda *a, **k: spawned.append(a))

        status = ensure_http_service(
            name="app", health_url="http://x/", command=["noop"],
        )
        assert status.state is State.ALREADY_RUNNING
        assert status.ok
        assert spawned == []          # the whole point: nothing started

    def test_starts_service_when_absent(self, monkeypatch):
        calls = {"alive": 0}

        def fake_alive(url, timeout=2.0):
            calls["alive"] += 1
            return calls["alive"] > 1      # dead at first, alive after launch

        monkeypatch.setattr(comp, "http_alive", fake_alive)
        monkeypatch.setattr(comp, "_spawn", lambda *a, **k: None)

        status = ensure_http_service(
            name="app", health_url="http://x/", command=["noop"], startup_timeout=5,
        )
        assert status.state is State.STARTED

    def test_reports_unavailable_when_it_never_answers(self, monkeypatch):
        monkeypatch.setattr(comp, "http_alive", lambda url, timeout=2.0: False)
        monkeypatch.setattr(comp, "_spawn", lambda *a, **k: None)

        status = ensure_http_service(
            name="app", health_url="http://x/", command=["noop"], startup_timeout=0.3,
        )
        assert status.state is State.UNAVAILABLE
        assert not status.ok

    def test_reports_unavailable_when_spawn_raises(self, monkeypatch):
        monkeypatch.setattr(comp, "http_alive", lambda url, timeout=2.0: False)

        def boom(*a, **k):
            raise OSError("no such executable")

        monkeypatch.setattr(comp, "_spawn", boom)
        status = ensure_http_service(
            name="app", health_url="http://x/", command=["noop"],
        )
        assert status.state is State.UNAVAILABLE
        assert "could not launch" in status.detail


class TestEnsureProcess:
    def test_adopts_running_process(self, monkeypatch):
        spawned: list = []
        monkeypatch.setattr(comp, "process_running", lambda name: True)
        monkeypatch.setattr(comp, "_spawn", lambda *a, **k: spawned.append(a))

        status = ensure_process(
            name="Premiere Pro", executable_name="x.exe", executable_path=Path("x.exe"),
        )
        assert status.state is State.ALREADY_RUNNING
        assert spawned == []

    def test_unavailable_when_not_installed(self, monkeypatch, tmp_path):
        monkeypatch.setattr(comp, "process_running", lambda name: False)
        status = ensure_process(
            name="Premiere Pro", executable_name="x.exe",
            executable_path=tmp_path / "missing.exe",
        )
        assert status.state is State.UNAVAILABLE

    def test_launches_when_installed_but_not_running(self, monkeypatch, tmp_path):
        exe = tmp_path / "prem.exe"
        exe.write_text("stub")
        monkeypatch.setattr(comp, "process_running", lambda name: False)
        monkeypatch.setattr(comp, "_spawn", lambda *a, **k: None)

        status = ensure_process(
            name="Premiere Pro", executable_name="prem.exe", executable_path=exe,
        )
        assert status.state is State.STARTED


# ─── Bridge probe ────────────────────────────────────────────────────────────

class TestBridgeAlive:
    """Contract verified against premiere-pro-mcp 1.14.4 MCPBridgeCEP/main.js:
    bridge-heartbeat.json, rewritten every 1000 ms, body
    {"protocolVersion": 1, "state": "running" | "waiting"}."""

    def _beat(self, tmp_path: Path, state: str = "running") -> Path:
        hb = tmp_path / HEARTBEAT_NAME
        hb.write_text(json.dumps({"protocolVersion": 1, "state": state}))
        return hb

    def test_running_heartbeat_means_alive(self, tmp_path):
        self._beat(tmp_path, "running")
        assert bridge_alive(BridgeConfig(directory=tmp_path)) is True

    def test_waiting_panel_is_not_ready(self, tmp_path):
        # Panel loaded but bridge not started — a different failure from
        # "no panel at all", and not something we can send commands to.
        self._beat(tmp_path, "waiting")
        assert bridge_alive(BridgeConfig(directory=tmp_path)) is False
        assert bridge_state(BridgeConfig(directory=tmp_path)) == "waiting"

    def test_waiting_counts_when_not_requiring_running(self, tmp_path):
        self._beat(tmp_path, "waiting")
        cfg = BridgeConfig(directory=tmp_path, require_running=False)
        assert bridge_alive(cfg) is True

    def test_stale_heartbeat_is_not_alive(self, tmp_path):
        # The connector deliberately leaves its last heartbeat behind on
        # shutdown, so existence alone would report a closed Premiere as ready.
        hb = self._beat(tmp_path, "running")
        old = time.time() - 600
        import os

        os.utime(hb, (old, old))
        assert bridge_alive(BridgeConfig(directory=tmp_path)) is False
        assert bridge_state(BridgeConfig(directory=tmp_path)) == "offline"

    def test_missing_directory_is_not_alive(self, tmp_path):
        assert bridge_alive(BridgeConfig(directory=tmp_path / "nope")) is False

    def test_malformed_heartbeat_is_not_alive(self, tmp_path):
        (tmp_path / HEARTBEAT_NAME).write_text("{ not json")
        assert bridge_alive(BridgeConfig(directory=tmp_path)) is False

    def test_http_probe_can_prove_liveness(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bridge_mod, "http_alive", lambda url, timeout=1.5: True)
        cfg = BridgeConfig(directory=tmp_path / "nope", health_url="http://bridge/health")
        assert bridge_alive(cfg) is True

    def test_ensure_bridge_degrades_with_guidance(self, tmp_path):
        status = ensure_bridge(
            timeout=0.2, config=BridgeConfig(directory=tmp_path / "nope"),
        )
        assert status.state is State.UNAVAILABLE
        assert "Extensions" in status.detail       # tells the user how to fix it

    def test_waiting_panel_gets_specific_guidance(self, tmp_path):
        self._beat(tmp_path, "waiting")
        status = ensure_bridge(timeout=0.2, config=BridgeConfig(directory=tmp_path))
        assert status.state is State.UNAVAILABLE
        assert "not started" in status.detail


# ─── Orchestration ───────────────────────────────────────────────────────────

class TestLaunch:
    def test_all_running_starts_nothing(self, monkeypatch, tmp_path):
        spawned: list = []
        monkeypatch.setattr(comp, "http_alive", lambda url, timeout=2.0: True)
        monkeypatch.setattr(comp, "process_running", lambda name: True)
        monkeypatch.setattr(comp, "_spawn", lambda *a, **k: spawned.append(a))
        monkeypatch.setattr(orch, "find_premiere_exe", lambda: tmp_path / "p.exe")
        monkeypatch.setattr(orch, "ensure_bridge", lambda **kw: ComponentStatus(
            "Premiere bridge", State.ALREADY_RUNNING,
        ))

        report = launch(LaunchOptions(project_root=tmp_path))
        assert spawned == []
        assert report.ready
        assert report.one_click_import

    def test_ready_without_bridge(self, monkeypatch, tmp_path):
        monkeypatch.setattr(comp, "http_alive", lambda url, timeout=2.0: True)
        monkeypatch.setattr(comp, "process_running", lambda name: True)
        monkeypatch.setattr(orch, "find_premiere_exe", lambda: tmp_path / "p.exe")
        monkeypatch.setattr(orch, "ensure_bridge", lambda **kw: ComponentStatus(
            "Premiere bridge", State.UNAVAILABLE, "no panel",
        ))

        report = launch(LaunchOptions(project_root=tmp_path))
        assert report.ready                    # app works
        assert not report.one_click_import     # but no automation
        assert "manual import" in report.summary()

    def test_not_ready_when_app_fails(self, monkeypatch, tmp_path):
        monkeypatch.setattr(comp, "http_alive", lambda url, timeout=2.0: False)
        monkeypatch.setattr(comp, "_spawn", lambda *a, **k: None)
        monkeypatch.setattr(comp, "process_running", lambda name: True)
        monkeypatch.setattr(orch, "ensure_bridge", lambda **kw: ComponentStatus(
            "Premiere bridge", State.SKIPPED,
        ))

        report = launch(LaunchOptions(
            project_root=tmp_path, start_premiere=False, wait_for_bridge=False,
            app_startup_timeout=0.3,
        ))
        assert not report.ready

    def test_premiere_can_be_skipped(self, monkeypatch, tmp_path):
        monkeypatch.setattr(comp, "http_alive", lambda url, timeout=2.0: True)
        report = launch(LaunchOptions(
            project_root=tmp_path, start_premiere=False, wait_for_bridge=False,
        ))
        names = {s.name: s.state for s in report.statuses}
        assert names["Premiere Pro"] is State.SKIPPED
        assert names["Premiere bridge"] is State.SKIPPED


# ─── Single instance ─────────────────────────────────────────────────────────

class TestSingleInstance:
    def test_second_entry_is_rejected(self):
        name = "Global\\ClipAutomationLauncherTest"
        with single_instance(name):
            with pytest.raises(AlreadyRunning):
                with single_instance(name):
                    pass

    def test_guard_is_released_after_use(self):
        name = "Global\\ClipAutomationLauncherTest2"
        with single_instance(name):
            pass
        with single_instance(name):      # must not raise
            pass
