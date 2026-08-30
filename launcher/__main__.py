"""
launcher/__main__.py — ``python -m launcher``

Brings the workspace up and opens the app in a browser. Exits non-zero only
when the app itself could not be started; a missing Premiere bridge is
reported but not treated as failure, because compilations still produce a
timeline for manual import without it.
"""

from __future__ import annotations

import argparse
import sys
import webbrowser
from pathlib import Path

from .bridge import bridge_alive
from .components import http_alive, process_running
from .orchestrator import (
    PREMIERE_EXE_NAME,
    LaunchOptions,
    find_premiere_exe,
    launch,
)
from .singleton import AlreadyRunning, single_instance


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m launcher",
        description="Start the Auto-Clipping workspace (app + Premiere + bridge).",
    )
    parser.add_argument("--host", default="127.0.0.1", help="app host (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=7860, help="app port (default 7860)")
    parser.add_argument("--no-premiere", action="store_true", help="do not start Premiere")
    parser.add_argument("--no-bridge-wait", action="store_true", help="do not wait for the bridge")
    parser.add_argument(
        "--bridge-timeout", type=float, default=60.0,
        help="seconds to wait for the Premiere bridge (default 60)",
    )
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser")
    parser.add_argument(
        "--status", action="store_true",
        help="report what is running and exit without starting anything",
    )
    return parser.parse_args(argv)


def _report_status(opts: LaunchOptions) -> int:
    app = http_alive(opts.app_health_url)
    premiere = process_running(PREMIERE_EXE_NAME)
    bridge = bridge_alive()
    exe = find_premiere_exe()

    print("Auto-Clipping workspace status")
    print(f"  app ({opts.app_health_url})  : {'running' if app else 'not running'}")
    print(f"  Premiere Pro process        : {'running' if premiere else 'not running'}")
    print(f"  Premiere bridge             : {'reachable' if bridge else 'not reachable'}")
    print(f"  Premiere executable         : {exe or 'not found'}")
    if premiere and not bridge:
        print(
            "\n  Premiere is open but not scriptable. Open the bridge panel and "
            "save it into the default workspace so it loads on every launch."
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    opts = LaunchOptions(
        app_host=args.host,
        app_port=args.port,
        project_root=Path(__file__).resolve().parent.parent,
        start_premiere=not args.no_premiere,
        wait_for_bridge=not args.no_bridge_wait,
        bridge_timeout=args.bridge_timeout,
        ninerouter_url=_ninerouter_health_url(),
    )

    if args.status:
        return _report_status(opts)

    try:
        with single_instance():
            report = launch(opts, log_fn=lambda m: print(m, flush=True))
    except AlreadyRunning as exc:
        print(f"{exc} Nothing to do.")
        return 0

    if not report.ready:
        print("\nThe app could not be started — see the messages above.")
        return 1

    if not args.no_browser:
        webbrowser.open(opts.app_health_url)
    return 0


def _ninerouter_health_url() -> str:
    """Derive a probe URL from the configured 9router base, if any."""
    try:
        import config

        base = getattr(config, "NINEROUTER_BASE_URL", "") or ""
    except Exception:  # noqa: BLE001 — config problems must not block launching
        return ""
    return base.rstrip("/").removesuffix("/v1") if base else ""


if __name__ == "__main__":
    sys.exit(main())
