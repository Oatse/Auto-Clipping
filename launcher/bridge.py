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

  ⚠ The exact liveness signal is deliberately pluggable. This project has not
  yet installed premiere-pro-mcp, so rather than guess its internals we probe
  for generic evidence (a fresh heartbeat file, or a bridge HTTP endpoint) and
  let both be configured. Confirm the real signal when the bridge is installed
  and set PREMIERE_BRIDGE_* accordingly.
"""

from __future__ import annotations

import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .components import ComponentStatus, State, http_alive, wait_until

LogFn = Callable[[str], None]

# premiere-pro-mcp's documented default shared directory.
DEFAULT_BRIDGE_DIR = Path(tempfile.gettempdir()) / "premiere-mcp-bridge"

# A heartbeat older than this means the panel is gone even though files remain.
HEARTBEAT_STALE_AFTER = 30.0


@dataclass(frozen=True)
class BridgeConfig:
    """Where to look for a live bridge."""

    directory: Path = DEFAULT_BRIDGE_DIR
    heartbeat_name: str = "heartbeat.json"
    health_url: str = ""          # optional HTTP probe, when the bridge exposes one
    stale_after: float = HEARTBEAT_STALE_AFTER

    @classmethod
    def from_env(cls) -> "BridgeConfig":
        directory = os.getenv("PREMIERE_TEMP_DIR") or os.getenv(
            "PREMIERE_BRIDGE_DIR", ""
        )
        return cls(
            directory=Path(directory) if directory else DEFAULT_BRIDGE_DIR,
            heartbeat_name=os.getenv("PREMIERE_BRIDGE_HEARTBEAT", "heartbeat.json"),
            health_url=os.getenv("PREMIERE_BRIDGE_HEALTH_URL", ""),
            stale_after=float(os.getenv("PREMIERE_BRIDGE_STALE_AFTER", HEARTBEAT_STALE_AFTER)),
        )


def bridge_alive(config: BridgeConfig | None = None) -> bool:
    """True when something is answering for the Premiere bridge.

    Two independent signals, either of which is sufficient:

    * an HTTP health endpoint, when the bridge exposes one;
    * a heartbeat file the in-Premiere panel refreshes — checked for
      *freshness*, because the file survives Premiere closing and a stale
      one would otherwise report a dead bridge as alive.
    """
    cfg = config or BridgeConfig.from_env()

    if cfg.health_url and http_alive(cfg.health_url, timeout=1.5):
        return True

    heartbeat = cfg.directory / cfg.heartbeat_name
    try:
        if heartbeat.is_file():
            age = time.time() - heartbeat.stat().st_mtime
            return age <= cfg.stale_after
    except OSError:
        return False
    return False


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

    return ComponentStatus(
        "Premiere bridge",
        State.UNAVAILABLE,
        "no response — open the bridge panel in Premiere and save it into the "
        "default workspace so it loads on every launch; the run will fall back "
        "to importing the generated timeline manually",
    )


__all__ = ["BridgeConfig", "bridge_alive", "ensure_bridge", "DEFAULT_BRIDGE_DIR"]
