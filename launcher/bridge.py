"""
launcher/bridge.py — Is Premiere actually reachable for automation?

A running Premiere process is **not** the same as a scriptable one. Premiere
takes commands through a CEP panel living inside it that polls a shared
directory for command files. If that panel is not loaded, Premiere is up and
completely deaf, so the launcher probes the bridge separately from the
process.

The panel cannot be opened from outside. Making the bridge come up on its own
requires a one-time setup inside Premiere — load the bridge panel once and
save it into the default workspace, so it loads with every launch.

Protocol confirmed against the installed connector (premiere-pro-mcp 1.14.4,
``MCPBridgeCEP/main.js``): the panel rewrites
``{tmp}/premiere-mcp-bridge/bridge-heartbeat.json`` once a second, publishing
it by atomic rename so a reader never sees partial JSON. The body is
``{"protocolVersion": 1, "state": "running" | "waiting"}`` — ``waiting`` means
the panel is loaded but the bridge has not been started, which is a different
thing from being ready to take commands.

On shutdown the panel deliberately leaves the last heartbeat in place, so
freshness — not mere existence — is what proves the bridge is alive.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .components import ComponentStatus, State, http_alive, wait_until

LogFn = Callable[[str], None]

# premiere-pro-mcp's shared directory and heartbeat file.
DEFAULT_BRIDGE_DIR = Path(tempfile.gettempdir()) / "premiere-mcp-bridge"
HEARTBEAT_NAME = "bridge-heartbeat.json"

# The panel writes every second; allow generous slack for a busy Premiere
# while still catching a connector that has actually stopped.
HEARTBEAT_STALE_AFTER = 10.0


@dataclass(frozen=True)
class BridgeConfig:
    """Where to look for a live bridge, and how strict to be."""

    directory: Path = DEFAULT_BRIDGE_DIR
    heartbeat_name: str = HEARTBEAT_NAME
    health_url: str = ""          # optional HTTP probe, when the bridge exposes one
    stale_after: float = HEARTBEAT_STALE_AFTER
    # When True, a panel that is loaded but idle ("waiting") does not count as
    # ready — only "running" does.
    require_running: bool = True

    @classmethod
    def from_env(cls) -> "BridgeConfig":
        # PREMIERE_TEMP_DIR is the connector's own variable; honour it so a
        # customised install is found without extra configuration.
        directory = os.getenv("PREMIERE_TEMP_DIR") or os.getenv(
            "PREMIERE_BRIDGE_DIR", ""
        )
        return cls(
            directory=Path(directory) if directory else DEFAULT_BRIDGE_DIR,
            heartbeat_name=os.getenv("PREMIERE_BRIDGE_HEARTBEAT", HEARTBEAT_NAME),
            health_url=os.getenv("PREMIERE_BRIDGE_HEALTH_URL", ""),
            stale_after=float(
                os.getenv("PREMIERE_BRIDGE_STALE_AFTER", HEARTBEAT_STALE_AFTER)
            ),
        )


def read_heartbeat(config: BridgeConfig | None = None) -> dict | None:
    """Return the current heartbeat, or None when absent/stale/unreadable.

    Staleness is enforced here because the connector leaves its last
    heartbeat behind when it stops; treating the file's existence as proof
    of life would report a closed Premiere as ready.
    """
    cfg = config or BridgeConfig.from_env()
    heartbeat = cfg.directory / cfg.heartbeat_name
    try:
        if not heartbeat.is_file():
            return None
        if time.time() - heartbeat.stat().st_mtime > cfg.stale_after:
            return None
        return json.loads(heartbeat.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # A torn read is possible in principle; the next poll will catch it.
        return None


def bridge_state(config: BridgeConfig | None = None) -> str:
    """``running`` | ``waiting`` | ``offline``."""
    beat = read_heartbeat(config)
    if beat is None:
        return "offline"
    state = str(beat.get("state") or "").lower()
    return state if state in ("running", "waiting") else "waiting"


def bridge_alive(config: BridgeConfig | None = None) -> bool:
    """True when the Premiere bridge can take commands.

    Either signal suffices: an HTTP health endpoint (when the bridge exposes
    one), or a fresh heartbeat from the in-Premiere panel.
    """
    cfg = config or BridgeConfig.from_env()

    if cfg.health_url and http_alive(cfg.health_url, timeout=1.5):
        return True

    state = bridge_state(cfg)
    if state == "running":
        return True
    return state == "waiting" and not cfg.require_running


def ensure_bridge(
    *,
    timeout: float = 60.0,
    config: BridgeConfig | None = None,
    log_fn: LogFn | None = None,
) -> ComponentStatus:
    """Wait for the bridge to come up, e.g. while Premiere finishes booting.

    Returns ``UNAVAILABLE`` rather than raising: the compilation flow has a
    perfectly good manual path (master + FCPXML on disk, File > Import), so a
    missing bridge degrades the experience instead of failing the run.
    """
    cfg = config or BridgeConfig.from_env()

    if bridge_alive(cfg):
        return ComponentStatus("Premiere bridge", State.ALREADY_RUNNING, str(cfg.directory))

    if log_fn:
        log_fn(f"Premiere bridge: waiting up to {timeout:.0f}s for the panel to come up...")

    if wait_until(lambda: bridge_alive(cfg), timeout=timeout):
        return ComponentStatus("Premiere bridge", State.STARTED, str(cfg.directory))

    # Distinguish the two failure modes: a panel that never loaded needs a
    # different fix from one that is loaded but idle.
    if bridge_state(cfg) == "waiting":
        detail = (
            "connector panel is open but the bridge is not started — press "
            "Start in Window > Extensions > MCP for Adobe Premiere Pro"
        )
    else:
        detail = (
            "no heartbeat — open Window > Extensions > MCP for Adobe Premiere "
            "Pro, then save it into the default workspace so it loads on every "
            "launch; the run falls back to importing the timeline manually"
        )
    return ComponentStatus("Premiere bridge", State.UNAVAILABLE, detail)


__all__ = [
    "BridgeConfig",
    "bridge_alive",
    "bridge_state",
    "read_heartbeat",
    "ensure_bridge",
    "DEFAULT_BRIDGE_DIR",
    "HEARTBEAT_NAME",
]
