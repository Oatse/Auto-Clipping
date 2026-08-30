"""Tests for web.services.startup.register_startup.

Starlette 1.0 removed ``add_event_handler``/``@app.on_event``, which made
``import web.server`` raise and the whole app unbootable. These pin the
replacement: hooks must actually run at startup, several must chain, and
an older Starlette must keep using the original API.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from web.services.startup import register_startup


def _app_with_probe() -> tuple[FastAPI, list[str]]:
    app = FastAPI()
    calls: list[str] = []

    @app.get("/ping")
    def ping() -> dict:
        return {"ok": True}

    return app, calls


class TestRegisterStartup:
    def test_hook_runs_on_startup(self):
        app, calls = _app_with_probe()

        async def hook() -> None:
            calls.append("ran")

        register_startup(app, hook)
        with TestClient(app) as client:
            assert client.get("/ping").status_code == 200
        assert calls == ["ran"]

    def test_multiple_hooks_all_run_in_registration_order(self):
        # web/server.py registers four restore hooks; every one must fire.
        app, calls = _app_with_probe()

        for name in ("first", "second", "third", "fourth"):
            async def hook(_n: str = name) -> None:
                calls.append(_n)

            register_startup(app, hook)

        with TestClient(app):
            pass
        assert calls == ["first", "second", "third", "fourth"]

    def test_sync_hook_is_supported(self):
        app, calls = _app_with_probe()
        register_startup(app, lambda: calls.append("sync"))
        with TestClient(app):
            pass
        assert calls == ["sync"]

    def test_hook_does_not_run_before_startup(self):
        app, calls = _app_with_probe()

        async def hook() -> None:
            calls.append("ran")

        register_startup(app, hook)
        assert calls == []          # only on startup, not at registration

    def test_uses_legacy_api_when_available(self):
        # An older Starlette still exposes add_event_handler; we must keep
        # using it there rather than rewriting the lifespan.
        seen: list[tuple] = []

        class LegacyApp:
            def add_event_handler(self, event, func):
                seen.append((event, func))

        async def hook() -> None:
            pass

        register_startup(LegacyApp(), hook)
        assert seen == [("startup", hook)]


class TestServerBoots:
    def test_web_server_imports_and_starts(self):
        # The regression that motivated the shim: importing web.server used
        # to raise AttributeError on Starlette 1.0.
        from web.server import app as real_app

        with TestClient(real_app) as client:
            assert client.get("/api/compilation/jobs").status_code == 200

    def test_compilation_router_is_mounted(self):
        from web.server import app as real_app

        paths = {getattr(r, "path", "") for r in real_app.routes}
        assert "/api/compilation/jobs" in paths
        assert "/api/compilation/jobs/{job_id}/fcpxml" in paths
