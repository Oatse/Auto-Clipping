"""
web/services/startup.py — Version-proof startup hook registration.

Each workspace router restores its jobs from disk when the server boots.
That used to be wired with ``app.add_event_handler("startup", ...)``, but
Starlette 1.0 removed the deprecated event-handler API (along with
``@app.on_event``), so importing ``web.server`` raised ``AttributeError``
and the app could not boot at all on current dependency versions.

``register_startup`` restores that capability by composing the app's
lifespan context, and still uses the old API when running against an
older Starlette so no pinned environment changes behaviour.
"""

from __future__ import annotations

import inspect
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable

StartupFn = Callable[[], Awaitable[None] | None]


def register_startup(app: Any, func: StartupFn) -> None:
    """Run ``func`` once when ``app`` starts.

    Hooks registered later run later: each call nests inside the lifespan
    installed before it, so the previous startup work finishes first. Both
    sync and async callables are accepted.
    """
    # Starlette < 1.0 — keep the original path so pinned installs behave
    # exactly as before.
    add_event_handler = getattr(app, "add_event_handler", None)
    if callable(add_event_handler):
        add_event_handler("startup", func)
        return

    router = app.router
    previous = router.lifespan_context

    @asynccontextmanager
    async def _lifespan(scoped_app: Any):
        async with previous(scoped_app) as state:
            result = func()
            if inspect.isawaitable(result):
                await result
            yield state

    router.lifespan_context = _lifespan


__all__ = ["register_startup"]
