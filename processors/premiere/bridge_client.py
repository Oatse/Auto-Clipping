"""
processors/premiere/bridge_client.py — Drive Premiere Pro from Python.

Speaks the file-IPC protocol of the premiere-pro-mcp CEP connector directly,
so the app can create a project and import a timeline without a Node MCP
client in the loop.

Protocol (verified against premiere-pro-mcp 1.14.4, ``MCPBridgeCEP/main.js``),
all inside ``{tmp}/premiere-mcp-bridge``:

    bridge-heartbeat.json   panel -> us, every 1000 ms, published by rename.
                            {"protocolVersion": 1, "state": "running"|"waiting"}
    cmd_{id}.jsx            us -> panel. ExtendScript, polled every 200 ms and
                            claimed by atomic rename.
    res_{id}.json           panel -> us, published by rename.
                            {"success": true, "data": ...} | {"success": false, "error": ...}
    busy_{id}.json          panel -> us, only when a script runs past 2 s.
                            Distinguishes "still working" from "panel is gone".

Two details are load-bearing:

* **Command files are staged and renamed.** The panel scans for ``cmd_*.jsx``
  every 200 ms, so writing in place risks it claiming a half-written script.
  The staging name deliberately does not match that glob.
* **The heartbeat is checked for freshness.** The connector leaves its last
  heartbeat behind when it stops, so existence alone would report a closed
  Premiere as ready.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

LogFn = Callable[[str], None]

DEFAULT_BRIDGE_DIR = Path(tempfile.gettempdir()) / "premiere-mcp-bridge"
HEARTBEAT_NAME = "bridge-heartbeat.json"

# The panel writes every second; allow slack for a busy Premiere while still
# catching a connector that has actually stopped.
HEARTBEAT_STALE_AFTER = 10.0

# The panel polls for commands every 200 ms, so there is no point checking
# for a response faster than that.
POLL_INTERVAL = 0.1
DEFAULT_TIMEOUT = 30.0


# ─── Heartbeat ───────────────────────────────────────────────────────────────


def bridge_dir() -> Path:
    """Shared directory, honouring the connector's own env var."""
    configured = os.getenv("PREMIERE_TEMP_DIR") or os.getenv("PREMIERE_BRIDGE_DIR")
    return Path(configured) if configured else DEFAULT_BRIDGE_DIR


def read_heartbeat(
    directory: Path | None = None,
    *,
    stale_after: float = HEARTBEAT_STALE_AFTER,
) -> dict | None:
    """Current heartbeat, or None when absent, stale, or unreadable."""
    path = (directory or bridge_dir()) / HEARTBEAT_NAME
    try:
        if not path.is_file():
            return None
        if time.time() - path.stat().st_mtime > stale_after:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def bridge_state(
    directory: Path | None = None,
    *,
    stale_after: float = HEARTBEAT_STALE_AFTER,
) -> str:
    """``running`` (ready) | ``waiting`` (panel open, bridge stopped) | ``offline``."""
    beat = read_heartbeat(directory, stale_after=stale_after)
    if beat is None:
        return "offline"
    state = str(beat.get("state") or "").lower()
    return state if state in ("running", "waiting") else "waiting"


# ─── Responses ───────────────────────────────────────────────────────────────


@dataclass
class BridgeResponse:
    """Outcome of one command."""

    success: bool
    data: Any = None
    error: str = ""

    @classmethod
    def failed(cls, message: str) -> "BridgeResponse":
        return cls(success=False, error=message)


class PremiereBridge:
    """Sends ExtendScript to Premiere through the connector panel."""

    def __init__(
        self,
        directory: Path | None = None,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        log_fn: LogFn | None = None,
    ):
        self.directory = directory or bridge_dir()
        self.timeout = timeout
        self._log = log_fn

    # ── availability ─────────────────────────────────────────────────────

    def state(self) -> str:
        return bridge_state(self.directory)

    def available(self) -> bool:
        """True only when the bridge can actually take commands."""
        return self.state() == "running"

    def unavailable_reason(self) -> str:
        """Why the bridge is not usable, phrased as the fix."""
        state = self.state()
        if state == "running":
            return ""
        if state == "waiting":
            return (
                "The Premiere connector panel is open but stopped. Press Start "
                "in Window > Extensions > MCP for Adobe Premiere Pro."
            )
        return (
            "No Premiere connector heartbeat. Open Premiere Pro, then "
            "Window > Extensions > MCP for Adobe Premiere Pro and press Start."
        )

    # ── command execution ────────────────────────────────────────────────

    def execute(
        self,
        code: str,
        *,
        timeout: float | None = None,
    ) -> BridgeResponse:
        """Run ExtendScript in Premiere and return its parsed response.

        ``code`` runs inside a try/catch IIFE and must ``return`` a JSON
        string; use :meth:`ok_expression` helpers or return one directly.
        """
        if not self.available():
            return BridgeResponse.failed(self.unavailable_reason())

        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return BridgeResponse.failed(f"cannot use bridge directory: {exc}")

        command_id = uuid.uuid4().hex[:12]
        command_path = self.directory / f"cmd_{command_id}.jsx"
        # Deliberately does not match the panel's cmd_*.jsx glob.
        staging_path = self.directory / f"staging_{command_id}.tmp"
        response_path = self.directory / f"res_{command_id}.json"
        busy_path = self.directory / f"busy_{command_id}.json"

        try:
            staging_path.write_text(_wrap(code), encoding="utf-8")
            staging_path.replace(command_path)   # atomic publish
        except OSError as exc:
            _quiet_unlink(staging_path)
            return BridgeResponse.failed(f"could not send command: {exc}")

        if self._log:
            self._log(f"Premiere bridge: sent {command_path.name}")

        try:
            return self._await_response(
                response_path, busy_path, command_path,
                timeout=timeout if timeout is not None else self.timeout,
            )
        finally:
            for leftover in (response_path, busy_path, command_path, staging_path):
                _quiet_unlink(leftover)

    def _await_response(
        self,
        response_path: Path,
        busy_path: Path,
        command_path: Path,
        *,
        timeout: float,
    ) -> BridgeResponse:
        """Poll for the response, tolerating long-running scripts."""
        deadline = time.monotonic() + timeout
        while True:
            if response_path.is_file():
                return _parse_response(response_path)

            if time.monotonic() >= deadline:
                # A busy file means the script is genuinely still running —
                # usually a modal dialog in Premiere — which is a different
                # problem from a dead panel, so say which one it is.
                if busy_path.is_file():
                    return BridgeResponse.failed(
                        f"Premiere is still executing after {timeout:.0f}s. "
                        "It may be showing a dialog that needs dismissing."
                    )
                if not command_path.exists():
                    return BridgeResponse.failed(
                        f"Command was picked up but produced no response within "
                        f"{timeout:.0f}s."
                    )
                return BridgeResponse.failed(
                    f"Premiere did not pick up the command within {timeout:.0f}s. "
                    + self.unavailable_reason()
                )

            time.sleep(POLL_INTERVAL)

    # ── high-level operations ────────────────────────────────────────────

    def ping(self) -> BridgeResponse:
        """Confirm Premiere is really answering, not just heartbeating."""
        return self.execute(
            'return \'{"success":true,"data":{"version":"\' + app.version + \'"}}\';'
        )

    def project_info(self) -> BridgeResponse:
        """Name and path of the open project."""
        return self.execute(
            "var p = app.project;"
            "if (!p) { return '{\"success\":false,\"error\":\"No project open\"}'; }"
            "return '{\"success\":true,\"data\":{\"name\":\"' + "
            "String(p.name).replace(/\"/g, '') + '\",\"path\":\"' + "
            "String(p.path).replace(/\\\\/g, '/').replace(/\"/g, '') + '\"}}';"
        )

    def new_project(self, path: Path) -> BridgeResponse:
        """Create a new Premiere project at ``path`` (.prproj)."""
        target = _escape(str(Path(path).resolve()))
        return self.execute(
            f'var made = app.newProject("{target}");'
            f"return '{{\"success\":true,\"data\":{{\"created\":' + "
            f"(made ? 'true' : 'false') + ',\"path\":\"{target}\"}}}}';"
        )

    def import_fcpxml(self, path: Path) -> BridgeResponse:
        """Import an FCP7 XML timeline into the project already open.

        Uses ``importFiles`` rather than ``app.openFCPXML``. openFCPXML takes
        (xmlPath, destinationProjectPath): with one argument it fails with
        "Not Enough Parameters", and used correctly it creates a SEPARATE
        project — not what someone with their project open wants. See
        :meth:`open_fcpxml_as_project` when a new project IS the goal.

        The path is resolved because Premiere has its own working directory,
        so a relative path silently resolves to nothing.
        """
        target = _escape(str(Path(path).resolve()))
        return self.execute(
            f'var f = new File("{target}");'
            "if (!f.exists) { return '{\"success\":false,\"error\":\"Timeline "
            "file not found\"}'; }"
            "var p = app.project;"
            "if (!p) { return '{\"success\":false,\"error\":\"No project open\"}'; }"
            "var before = p.sequences.numSequences;"
            f'var okc = p.importFiles(["{target}"], true, p.rootItem, false);'
            "var added = p.sequences.numSequences - before;"
            "if (!okc) { return '{\"success\":false,\"error\":\"Premiere refused "
            "the import\"}'; }"
            "return '{\"success\":true,\"data\":{\"imported\":true,"
            "\"sequencesAdded\":' + added + '}}';",
            timeout=max(self.timeout, 120.0),   # importing can be slow
        )

    def open_fcpxml_as_project(
        self, xml_path: Path, project_path: Path
    ) -> BridgeResponse:
        """Create a NEW project from an FCP7 XML timeline.

        The two-argument form openFCPXML actually requires. Use this only when
        a separate project is wanted; :meth:`import_fcpxml` is the usual call.
        """
        xml = _escape(str(Path(xml_path).resolve()))
        project = _escape(str(Path(project_path).resolve()))
        return self.execute(
            f'app.openFCPXML("{xml}", "{project}");'
            "return '{\"success\":true,\"data\":{\"created\":true,"
            f"\"project\":\"{project}\"}}}}';",
            timeout=max(self.timeout, 120.0),
        )


# ─── helpers ─────────────────────────────────────────────────────────────────


def _wrap(code: str) -> str:
    """Wrap ExtendScript in the try/catch IIFE the panel expects.

    The connector's own ``__result``/``__error`` helpers are loaded by a
    bootstrap the Node server prepends, which we do not use — so scripts here
    return their JSON directly and the catch builds its own error envelope.
    """
    return (
        "(function() {\n"
        "  try {\n"
        f"    {code}\n"
        "  } catch (e) {\n"
        "    return '{\"success\":false,\"error\":\"' + "
        "String(e).replace(/\\\\/g, '/').replace(/\"/g, \"'\") + '\"}';\n"
        "  }\n"
        "})();"
    )


def _escape(value: str) -> str:
    """Escape a string for embedding in an ExtendScript double-quoted literal."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _parse_response(path: Path) -> BridgeResponse:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return BridgeResponse.failed(f"unreadable response: {exc}")

    if not isinstance(payload, dict):
        return BridgeResponse(success=True, data=payload)
    if payload.get("success") is False:
        return BridgeResponse.failed(str(payload.get("error") or "unknown error"))
    return BridgeResponse(success=True, data=payload.get("data", payload))


def _quiet_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


__all__ = [
    "PremiereBridge",
    "BridgeResponse",
    "bridge_dir",
    "bridge_state",
    "read_heartbeat",
    "DEFAULT_BRIDGE_DIR",
    "HEARTBEAT_NAME",
    "HEARTBEAT_STALE_AFTER",
]
