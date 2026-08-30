"""
launcher/orchestrator.py — Bring the whole workspace up with one command.

Starts (or adopts) everything a compilation run needs and reports what it
found. Two properties matter:

**Idempotent.** Each component is probed before anything is launched, so
running the launcher a second time adopts what is already alive instead of
creating a duplicate app server, a second Premiere window, or another proxy.

**Degrading.** Only the app is required. Premiere and its bridge failing to
come up costs the one-click import, not the run: the pipeline still writes
the master and timeline to disk for a manual File > Import.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .bridge import BridgeConfig, ensure_bridge
from .components import (
    ComponentStatus,
    State,
    ensure_http_service,
    ensure_process,
    http_alive,
)

LogFn = Callable[[str], None]

PREMIERE_EXE_CANDIDATES = (
    r"C:\Program Files\Adobe\Adobe Premiere Pro 2025\Adobe Premiere Pro.exe",
    r"C:\Program Files\Adobe\Adobe Premiere Pro 2024\Adobe Premiere Pro.exe",
    r"C:\Program Files\Adobe\Adobe Premiere Pro 2023\Adobe Premiere Pro.exe",
    r"C:\Program Files\Adobe\Adobe Premiere Pro 2022\Adobe Premiere Pro.exe",
)
PREMIERE_EXE_NAME = "Adobe Premiere Pro.exe"


@dataclass
class LaunchOptions:
    app_port: int = 7860
    app_host: str = "127.0.0.1"
    project_root: Path = field(default_factory=lambda: Path(__file__).resolve().parent.parent)
    start_premiere: bool = True
    wait_for_bridge: bool = True
    bridge_timeout: float = 60.0
    app_startup_timeout: float = 45.0
    ninerouter_url: str = ""          # probed only, never started for the user
    premiere_exe: Path | None = None

    @property
    def app_health_url(self) -> str:
        return f"http://{self.app_host}:{self.app_port}/"


@dataclass
class LaunchReport:
    statuses: list[ComponentStatus] = field(default_factory=list)

    @property
    def app(self) -> ComponentStatus | None:
        return next((s for s in self.statuses if s.name == "Auto-Clipping app"), None)

    @property
    def bridge(self) -> ComponentStatus | None:
        return next((s for s in self.statuses if s.name == "Premiere bridge"), None)

    @property
    def ready(self) -> bool:
        """True when the app is usable, with or without Premiere automation."""
        app = self.app
        return bool(app and app.ok)

    @property
    def one_click_import(self) -> bool:
        """True when Premiere can be driven directly."""
        bridge = self.bridge
        return bool(bridge and bridge.ok)

    def summary(self) -> str:
        lines = [str(s) for s in self.statuses]
        if self.ready and not self.one_click_import:
            lines.append(
                "NOTE: Premiere automation is unavailable — compilations will "
                "still produce master + timeline files for manual import."
            )
        return "\n".join(lines)


def find_premiere_exe() -> Path | None:
    """Newest installed Premiere, or None."""
    for candidate in PREMIERE_EXE_CANDIDATES:
        path = Path(candidate)
        if path.exists():
            return path
    return None


def launch(
    options: LaunchOptions | None = None,
    *,
    log_fn: LogFn | None = None,
) -> LaunchReport:
    """Ensure every component is up; adopt whatever already is."""
    opts = options or LaunchOptions()
    report = LaunchReport()

    def log(message: str) -> None:
        if log_fn:
            log_fn(message)

    # 1. The app itself — the only hard requirement.
    report.statuses.append(
        ensure_http_service(
            name="Auto-Clipping app",
            health_url=opts.app_health_url,
            command=[
                sys.executable, "run_web.py",
                "--host", opts.app_host, "--port", str(opts.app_port),
            ],
            cwd=opts.project_root,
            startup_timeout=opts.app_startup_timeout,
            log_fn=log_fn,
        )
    )

    # 2. 9router proxy — probed only. It is the user's own local process and
    #    starting it here would guess at their setup.
    if opts.ninerouter_url:
        alive = http_alive(opts.ninerouter_url, timeout=1.5)
        report.statuses.append(
            ComponentStatus(
                "9router proxy",
                State.ALREADY_RUNNING if alive else State.UNAVAILABLE,
                opts.ninerouter_url if alive
                else "not reachable — Claude/Codex backends will fail; Gemini is unaffected",
            )
        )

    # 3. Premiere.
    if opts.start_premiere:
        exe = opts.premiere_exe or find_premiere_exe()
        report.statuses.append(
            ensure_process(
                name="Premiere Pro",
                executable_name=PREMIERE_EXE_NAME,
                executable_path=exe,
                log_fn=log_fn,
            )
        )
    else:
        report.statuses.append(ComponentStatus("Premiere Pro", State.SKIPPED))

    # 4. The bridge — separate from the process, because a booted Premiere
    #    without its panel loaded cannot take commands.
    if opts.start_premiere and opts.wait_for_bridge:
        report.statuses.append(
            ensure_bridge(
                timeout=opts.bridge_timeout,
                config=BridgeConfig.from_env(),
                log_fn=log_fn,
            )
        )
    else:
        report.statuses.append(ComponentStatus("Premiere bridge", State.SKIPPED))

    log(report.summary())
    return report


__all__ = [
    "LaunchOptions",
    "LaunchReport",
    "launch",
    "find_premiere_exe",
    "PREMIERE_EXE_NAME",
]
