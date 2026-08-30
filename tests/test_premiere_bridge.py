"""Tests for the Python -> Premiere bridge client and panel installer.

The bridge protocol is file-based, so it can be exercised end to end against a
fake "panel" that answers command files in a temp directory. Two behaviours
are load-bearing and pinned here:

  * commands are published atomically (the real panel polls every 200 ms and
    would otherwise claim a half-written script);
  * the heartbeat is judged by freshness, because the connector deliberately
    leaves its last one behind when it stops.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from launcher import panel_install
from processors.premiere.bridge_client import (
    HEARTBEAT_NAME,
    BridgeResponse,
    PremiereBridge,
    bridge_state,
    read_heartbeat,
)


def _beat(directory: Path, state: str = "running") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / HEARTBEAT_NAME
    path.write_text(json.dumps({"protocolVersion": 1, "state": state}))
    return path


class FakePanel:
    """Answers command files the way the real CEP panel does."""

    def __init__(self, directory: Path, response: dict | None = None, delay: float = 0.0):
        self.directory = directory
        self.response = response if response is not None else {"success": True, "data": {"ok": 1}}
        self.delay = delay
        self.seen: list[str] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "FakePanel":
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                for path in sorted(self.directory.glob("cmd_*.jsx")):
                    self.seen.append(path.read_text(encoding="utf-8"))
                    command_id = path.stem.replace("cmd_", "")
                    path.unlink(missing_ok=True)
                    if self.delay:
                        time.sleep(self.delay)
                    (self.directory / f"res_{command_id}.json").write_text(
                        json.dumps(self.response), encoding="utf-8",
                    )
            except OSError:
                pass
            time.sleep(0.02)


# ─── Heartbeat ───────────────────────────────────────────────────────────────

class TestHeartbeat:
    def test_running_state(self, tmp_path):
        _beat(tmp_path, "running")
        assert bridge_state(tmp_path) == "running"
        assert PremiereBridge(tmp_path).available() is True

    def test_waiting_is_not_available(self, tmp_path):
        _beat(tmp_path, "waiting")
        assert bridge_state(tmp_path) == "waiting"
        bridge = PremiereBridge(tmp_path)
        assert bridge.available() is False
        assert "Press Start" in bridge.unavailable_reason()

    def test_stale_heartbeat_is_offline(self, tmp_path):
        path = _beat(tmp_path, "running")
        old = time.time() - 600
        os.utime(path, (old, old))
        assert bridge_state(tmp_path) == "offline"

    def test_missing_is_offline(self, tmp_path):
        assert bridge_state(tmp_path / "nope") == "offline"
        assert read_heartbeat(tmp_path / "nope") is None

    def test_malformed_is_offline(self, tmp_path):
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / HEARTBEAT_NAME).write_text("{ broken")
        assert bridge_state(tmp_path) == "offline"


# ─── Command round-trip ──────────────────────────────────────────────────────

class TestExecute:
    def test_refuses_when_bridge_offline(self, tmp_path):
        result = PremiereBridge(tmp_path).execute("return '{}';")
        assert result.success is False
        assert "connector" in result.error.lower()

    def test_successful_round_trip(self, tmp_path):
        _beat(tmp_path)
        with FakePanel(tmp_path, {"success": True, "data": {"version": "23.2.0"}}):
            result = PremiereBridge(tmp_path, timeout=5).execute("return '{}';")
        assert result.success is True
        assert result.data == {"version": "23.2.0"}

    def test_error_response_is_surfaced(self, tmp_path):
        _beat(tmp_path)
        with FakePanel(tmp_path, {"success": False, "error": "No project open"}):
            result = PremiereBridge(tmp_path, timeout=5).execute("return '{}';")
        assert result.success is False
        assert result.error == "No project open"

    def test_timeout_when_panel_never_answers(self, tmp_path):
        _beat(tmp_path)
        result = PremiereBridge(tmp_path, timeout=0.4).execute("return '{}';")
        assert result.success is False
        assert "did not pick up" in result.error

    def test_command_is_published_atomically(self, tmp_path):
        # A staging file must never match the panel's cmd_*.jsx glob, or the
        # panel could claim a partially-written script.
        _beat(tmp_path)
        with FakePanel(tmp_path) as panel:
            PremiereBridge(tmp_path, timeout=5).execute("return '{}';")
        assert panel.seen, "panel never saw the command"
        assert all("staging" not in text for text in panel.seen)

    def test_scratch_files_are_cleaned_up(self, tmp_path):
        _beat(tmp_path)
        with FakePanel(tmp_path):
            PremiereBridge(tmp_path, timeout=5).execute("return '{}';")
        leftovers = [
            p.name for p in tmp_path.iterdir()
            if p.name != HEARTBEAT_NAME
        ]
        assert leftovers == []

    def test_slow_script_still_completes(self, tmp_path):
        _beat(tmp_path)
        with FakePanel(tmp_path, delay=0.3):
            result = PremiereBridge(tmp_path, timeout=5).execute("return '{}';")
        assert result.success is True


# ─── High-level operations build the right script ────────────────────────────

class TestOperations:
    def _capture(self, tmp_path, call):
        _beat(tmp_path)
        with FakePanel(tmp_path) as panel:
            call(PremiereBridge(tmp_path, timeout=5))
        return panel.seen[0]

    def test_ping_reads_app_version(self, tmp_path):
        script = self._capture(tmp_path, lambda b: b.ping())
        assert "app.version" in script

    def test_import_uses_openFCPXML(self, tmp_path):
        script = self._capture(
            tmp_path, lambda b: b.import_fcpxml(Path("D:/out/compilation.xml")),
        )
        assert "app.openFCPXML" in script
        assert "compilation.xml" in script

    def test_new_project_uses_newProject(self, tmp_path):
        script = self._capture(
            tmp_path, lambda b: b.new_project(Path("D:/out/My Project.prproj")),
        )
        assert "app.newProject" in script

    def test_windows_paths_are_escaped(self, tmp_path):
        # A raw backslash would terminate the ExtendScript string literal.
        script = self._capture(
            tmp_path, lambda b: b.import_fcpxml(Path("D:/out/compilation.xml")),
        )
        assert "\\\\" in script or "/" in script

    def test_script_is_wrapped_in_try_catch(self, tmp_path):
        script = self._capture(tmp_path, lambda b: b.ping())
        assert script.startswith("(function()")
        assert "catch" in script


# ─── Panel installer ─────────────────────────────────────────────────────────

class TestPanelInstall:
    def test_source_panel_is_complete(self):
        source = panel_install.source_dir()
        for required in ("CSXS/manifest.xml", "index.html", "main.js",
                         "host.jsx", "styles.css", "CSInterface.js"):
            assert (source / required).is_file(), f"missing {required}"

    def test_shipped_manifest_is_valid_xml(self):
        # Regression: a "--" inside an XML comment is illegal, and CEP
        # responded by rejecting the whole extension so the panel silently
        # never appeared in Window > Extensions.
        panel_install.validate_manifest(
            panel_install.source_dir() / "CSXS" / "manifest.xml"
        )

    def test_double_hyphen_comment_is_rejected(self, tmp_path):
        bad = tmp_path / "manifest.xml"
        bad.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<!-- flag: --enable-nodejs -->\n"
            "<ExtensionManifest/>\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError) as excinfo:
            panel_install.validate_manifest(bad)
        assert "hyphen" in str(excinfo.value).lower()

    def test_install_refuses_an_invalid_manifest(self, tmp_path, monkeypatch):
        source = tmp_path / "panel"
        (source / "CSXS").mkdir(parents=True)
        (source / "CSXS" / "manifest.xml").write_text("<broken", encoding="utf-8")
        monkeypatch.setattr(panel_install, "source_dir", lambda root=None: source)
        monkeypatch.setattr(
            panel_install, "extensions_dir", lambda: tmp_path / "extensions",
        )
        with pytest.raises(ValueError):
            panel_install.install()

    def test_install_and_uninstall(self, tmp_path, monkeypatch):
        fake_ext = tmp_path / "extensions"
        monkeypatch.setattr(panel_install, "extensions_dir", lambda: fake_ext)

        result = panel_install.install()
        assert result.installed
        assert (fake_ext / panel_install.PANEL_ID / "CSXS" / "manifest.xml").is_file()
        assert (fake_ext / panel_install.PANEL_ID / "main.js").is_file()

        assert panel_install.uninstall() is True
        assert not (fake_ext / panel_install.PANEL_ID).exists()
        assert panel_install.uninstall() is False

    def test_reinstall_replaces_cleanly(self, tmp_path, monkeypatch):
        fake_ext = tmp_path / "extensions"
        monkeypatch.setattr(panel_install, "extensions_dir", lambda: fake_ext)

        panel_install.install()
        stray = fake_ext / panel_install.PANEL_ID / "stale.js"
        stray.write_text("old")
        panel_install.install()
        assert not stray.exists()          # replaced, not merged

    def test_status_warns_when_debug_mode_off(self, tmp_path, monkeypatch):
        fake_ext = tmp_path / "extensions"
        monkeypatch.setattr(panel_install, "extensions_dir", lambda: fake_ext)
        monkeypatch.setattr(panel_install, "debug_mode_enabled", lambda: False)

        panel_install.install()
        result = panel_install.status()
        assert result.installed
        assert not result.debug_mode
        assert "PlayerDebugMode" in result.message
