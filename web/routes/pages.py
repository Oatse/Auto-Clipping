"""
web/routes/pages.py — HTML page routes (Jinja2 templates).

Six page routes, each just returns a template response with an
``active`` flag for the navigation highlight. No Job state, no shared
globals beyond ``templates``. Pulled out of ``web/server.py`` so adding
a new workspace page no longer requires touching the main server file.

Usage:

    from web.routes.pages import build_page_router
    app.include_router(build_page_router(templates))

The factory pattern is used because the ``Jinja2Templates`` instance
is created in ``web/server.py`` against the project's ``templates/``
directory. Passing it in keeps this module pure (no module-level
filesystem access at import time).
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates


def build_page_router(templates: Jinja2Templates) -> APIRouter:
    """Build the page router bound to the given ``templates`` instance.

    Each page handler is a one-liner that delegates to
    ``templates.TemplateResponse``. The ``active`` context flag drives
    the navigation highlight in ``base.html``; values must match the
    nav link selectors there.
    """
    router = APIRouter()

    @router.get("/", response_class=HTMLResponse)
    async def page_home(request: Request):
        return templates.TemplateResponse(
            request, "pages/home.html", {"active": "home"},
        )

    @router.get("/auto-subtitle", response_class=HTMLResponse)
    async def page_auto_subtitle(request: Request):
        return templates.TemplateResponse(
            request, "pages/auto_subtitle.html", {"active": "subtitle"},
        )

    @router.get("/clip-finder", response_class=HTMLResponse)
    async def page_clip_finder(request: Request):
        return templates.TemplateResponse(
            request, "pages/clip_finder.html", {"active": "clipfinder"},
        )

    @router.get("/short-maker", response_class=HTMLResponse)
    async def page_short_maker(request: Request):
        return templates.TemplateResponse(
            request, "pages/short_maker.html", {"active": "shortmaker"},
        )

    @router.get("/all-in", response_class=HTMLResponse)
    async def page_all_in(request: Request):
        return templates.TemplateResponse(
            request, "pages/all_in.html", {"active": "allin"},
        )

    @router.get("/editor", response_class=HTMLResponse)
    @router.get("/editor/{job_id}", response_class=HTMLResponse)
    async def page_editor(request: Request, job_id: str | None = None):
        return templates.TemplateResponse(
            request,
            "pages/editor.html",
            {"active": "editor", "job_id": job_id},
        )

    return router


__all__ = ["build_page_router"]
