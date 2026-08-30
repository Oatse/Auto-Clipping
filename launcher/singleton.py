"""
launcher/singleton.py — Stop two launchers from racing each other.

Without this, double-clicking the shortcut twice runs two launchers that
both probe, both see nothing, and both start a copy of everything. The guard
is deliberately *not* a plain lock file: a crashed launcher would leave one
behind and permanently block every future start. A Windows named mutex and a
POSIX file lock are both released by the OS when the process dies.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from typing import Iterator

MUTEX_NAME = "Global\\ClipAutomationLauncher"


class AlreadyRunning(RuntimeError):
    """Another launcher instance holds the guard."""


@contextmanager
def single_instance(name: str = MUTEX_NAME) -> Iterator[None]:
    """Hold a cross-process guard for the duration of the block.

    Raises :class:`AlreadyRunning` when another launcher already holds it.
    Falls through without guarding on platforms where neither mechanism is
    available, since blocking the launcher entirely would be worse than the
    rare double-start it prevents.
    """
    if sys.platform == "win32":
        handle = _acquire_windows(name)
        try:
            yield
        finally:
            _release_windows(handle)
        return

    lock = _acquire_posix(name)
    try:
        yield
    finally:
        _release_posix(lock)


# ─── Windows ─────────────────────────────────────────────────────────────────


def _acquire_windows(name: str):
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:  # pragma: no cover — Windows always has ctypes
        return None

    ERROR_ALREADY_EXISTS = 183
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = [wintypes.LPCVOID, wintypes.BOOL, wintypes.LPCWSTR]

    handle = kernel32.CreateMutexW(None, True, name)
    if not handle:
        return None
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        raise AlreadyRunning(
            "Another launcher is already starting the workspace."
        )
    return handle


def _release_windows(handle) -> None:
    if not handle:
        return
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.ReleaseMutex(handle)
    kernel32.CloseHandle(handle)


# ─── POSIX ───────────────────────────────────────────────────────────────────


def _acquire_posix(name: str):
    try:
        import fcntl
        import tempfile
        from pathlib import Path

        safe = name.replace("\\", "_").replace("/", "_")
        path = Path(tempfile.gettempdir()) / f"{safe}.lock"
        handle = path.open("w")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise AlreadyRunning(
                "Another launcher is already starting the workspace."
            )
        return handle
    except ImportError:
        return None


def _release_posix(handle) -> None:
    if handle is None:
        return
    try:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


__all__ = ["single_instance", "AlreadyRunning", "MUTEX_NAME"]
