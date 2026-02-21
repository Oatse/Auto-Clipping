"""
run_web.py — Launch the CLIP-AUTOMATION web interface.

Usage:
    python run_web.py
    python run_web.py --port 8080
    python run_web.py --host 0.0.0.0 --port 8000
"""

import argparse
import sys
import webbrowser
from pathlib import Path

import uvicorn

# ── Windows fix: suppress spurious ConnectionResetError (WinError 10054) ──────
# Python's asyncio ProactorEventLoop on Windows calls socket.shutdown() during
# connection cleanup even when the remote host has already forcibly closed the
# socket (e.g. browser seeking/closing a video stream). This raises WinError
# 10054 inside _call_connection_lost, polluting the log with harmless noise.
if sys.platform == "win32":
    import asyncio.proactor_events as _pe

    _orig_conn_lost = _pe._ProactorBasePipeTransport._call_connection_lost

    def _patched_call_connection_lost(self, exc):
        try:
            _orig_conn_lost(self, exc)
        except ConnectionResetError:
            pass  # WinError 10054 — client disconnected, safe to ignore

    _pe._ProactorBasePipeTransport._call_connection_lost = _patched_call_connection_lost
# ──────────────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="CLIP-AUTOMATION Web UI")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=7860, help="Port to listen on (default: 7860)")
    parser.add_argument("--no-browser", action="store_true", help="Don't open browser automatically")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload (dev mode)")
    args = parser.parse_args()

    url = f"http://{args.host}:{args.port}"
    print(f"\n{'='*50}")
    print(f"  CLIP-AUTOMATION Web UI")
    print(f"  Running at: {url}")
    print(f"{'='*50}\n")

    if not args.no_browser:
        import threading
        import time
        def open_browser():
            time.sleep(1.5)
            webbrowser.open(url)
        threading.Thread(target=open_browser, daemon=True).start()

    uvicorn.run(
        "web.server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
