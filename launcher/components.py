"""
launcher/components.py — Idempotent "make sure X is running" guards.

Every guard answers the same question — *is this component already
alive?* — and only starts something when the answer is no. Running the
launcher twice must therefore be a no-op rather than a second copy of the
app, a second Premiere window, or a duplicated proxy.

Liveness is decided by a **live probe** (an HTTP health check, a process
lookup), never by a PID file or lock file. A stale lock left behind by a
crash would claim a dead component is healthy and block the restart that
would actually fix it; a probe simply says "no answer" and self-heals.
"""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Sequence

LogFn = Callable[[str], None]


class State(str, Enum):
    """Outcome of ensuring one component."""

    ALREADY_RUNNING = "already_running"   # found alive; nothing started
    STARTED = "started"                   # we launched it
    UNAVAILABLE = "unavailable"           # could not be started
    SKIPPED = "skipped"                   # not needed for this run


@dataclass
class ComponentStatus:
    name: str
    state: State
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.state in (State.ALREADY_RUNNING, State.STARTED)

    def __str__(self) -> str:
        mark = {
            State.ALREADY_RUNNING: "already running",
            State.STARTED: "started",
            State.UNAVAILABLE: "UNAVAILABLE",
            State.SKIPPED: "skipped",
        }[self.state]
        return f"{self.name}: {mark}" + (f" ({self.detail})" if self.detail else "")


# ─── Probes ──────────────────────────────────────────────────────────────────


def http_alive(url: str, timeout: float = 2.0) -> bool:
    """True when ``url`` answers at all.

    Any HTTP status counts as alive — a 404 still proves something is
    listening on that port, which is the question being asked.
    """
    try:
        import httpx

        httpx.get(url, timeout=timeout)
        return True
    except Exception:  # noqa: BLE001 — unreachable is the expected negative
        return False


def process_running(executable_name: str) -> bool:
    """True when a process with this executable name exists.

    Uses psutil when installed and falls back to ``tasklist`` on Windows,
    so the launcher does not add a hard dependency.
    """
    try:
        import psutil

        target = executable_name.lower()
        for proc in psutil.process_iter(["name"]):
            name = (proc.info.get("name") or "").lower()
            if name == target:
                return True
        return False
    except ImportError:
        pass

    if sys.platform != "win32":
        return False
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {executable_name}"],
            capture_output=True, text=True, timeout=15, check=False,
        )
        return executable_name.lower() in (out.stdout or "").lower()
    except Exception:  # noqa: BLE001
        return False


def wait_until(
    predicate: Callable[[], bool],
    *,
    timeout: float = 60.0,
    initial_interval: float = 0.5,
    max_interval: float = 4.0,
) -> bool:
    """Poll ``predicate`` with backoff until true or ``timeout`` elapses.

    Backoff keeps a slow Premiere launch from being hammered with probes
    while still noticing a fast start almost immediately.
    """
    deadline = time.monotonic() + timeout
    interval = initial_interval
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(interval, max(0.0, deadline - time.monotonic())))
        interval = min(interval * 1.5, max_interval)


# ─── Guards ──────────────────────────────────────────────────────────────────


def ensure_http_service(
    *,
    name: str,
    health_url: str,
    command: Sequence[str],
    cwd: Path | None = None,
    startup_timeout: float = 45.0,
    log_fn: LogFn | None = None,
) -> ComponentStatus:
    """Ensure an HTTP service is reachable, starting it only if it is not."""
    if http_alive(health_url):
        return ComponentStatus(name, State.ALREADY_RUNNING, health_url)

    if log_fn:
        log_fn(f"{name}: not responding at {health_url} — starting it")
    try:
        _spawn(command, cwd)
    except Exception as exc:  # noqa: BLE001
        return ComponentStatus(name, State.UNAVAILABLE, f"could not launch: {exc}")

    if wait_until(lambda: http_alive(health_url), timeout=startup_timeout):
        return ComponentStatus(name, State.STARTED, health_url)
    return ComponentStatus(
        name, State.UNAVAILABLE, f"did not answer within {startup_timeout:.0f}s",
    )


def ensure_process(
    *,
    name: str,
    executable_name: str,
    executable_path: Path | None,
    log_fn: LogFn | None = None,
) -> ComponentStatus:
    """Ensure a desktop application is running, launching it if needed.

    Note the deliberate limit: this proves the *process* exists, which is
    not the same as it being ready to take commands. Premiere in
    particular boots for tens of seconds and only becomes scriptable once
    its bridge panel loads — see ``launcher.bridge``.
    """
    if process_running(executable_name):
        return ComponentStatus(name, State.ALREADY_RUNNING, executable_name)

    if executable_path is None or not Path(executable_path).exists():
        return ComponentStatus(
            name, State.UNAVAILABLE,
            f"not running and executable not found: {executable_path}",
        )

    if log_fn:
        log_fn(f"{name}: not running — launching {Path(executable_path).name}")
    try:
        _spawn([str(executable_path)], None)
    except Exception as exc:  # noqa: BLE001
        return ComponentStatus(name, State.UNAVAILABLE, f"could not launch: {exc}")
    return ComponentStatus(name, State.STARTED, str(executable_path))


def _spawn(command: Sequence[str], cwd: Path | None) -> None:
    """Start a detached child that outlives the launcher."""
    kwargs: dict = {"cwd": str(cwd) if cwd else None}
    if sys.platform == "win32":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP so closing the
        # launcher console does not take the service down with it.
        kwargs["creationflags"] = 0x00000008 | 0x00000200
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(list(command), **kwargs)  # noqa: S603


__all__ = [
    "State",
    "ComponentStatus",
    "http_alive",
    "process_running",
    "wait_until",
    "ensure_http_service",
    "ensure_process",
]
