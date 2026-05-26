"""
web/routes/system.py — System info + API quota probes.

Three read-only endpoints with no dependency on the in-memory job
dicts:

  - GET /api/system           → CUDA, package availability, key flags
  - GET /api/elevenlabs/quota → per-key character usage
  - GET /api/gemini/quota     → per-key validity probe

Pulled out of ``web/server.py`` first because they're the most
isolated surface — no Job state, no shared globals beyond
``config``. The whole router can be deleted and re-implemented
without affecting any other workspace.

Mounted by ``web/server.py`` via:

    from web.routes.system import router as system_router
    app.include_router(system_router)
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException

import config


# torch is optional — only used for surfacing GPU info on /api/system.
# Keep the import here defensive so removing whisperx doesn't drag in
# a hard transitive dependency.
try:
    import torch  # type: ignore
    _HAS_TORCH = True
except ImportError:  # pragma: no cover
    torch = None  # type: ignore
    _HAS_TORCH = False


router = APIRouter()


# ─── Internal helpers ────────────────────────────────────────────────────────


def _check_package(name: str) -> bool:
    """Return True if ``name`` can be imported in this venv."""
    try:
        __import__(name)
        return True
    except ImportError:
        return False


def _check_ffmpeg() -> bool:
    """Return True when ffmpeg is reachable via PATH or ``config.FFMPEG_PATH``."""
    if shutil.which("ffmpeg") is not None:
        return True
    try:
        ffmpeg_path = getattr(config, "FFMPEG_PATH", None)
        if ffmpeg_path and Path(ffmpeg_path).is_file():
            return True
    except Exception:  # noqa: BLE001 — best-effort probe
        pass
    return False


# ─── /api/system ─────────────────────────────────────────────────────────────


@router.get("/api/system")
async def get_system_info() -> dict:
    """Return CUDA state, package availability, and API-key flags.

    Used by the Recent Jobs panel in the UI to surface hard requirements
    (no ElevenLabs key → can't transcribe) and soft fallbacks (no DeepL
    key → Gemini-only translation).
    """
    if _HAS_TORCH and torch is not None:
        cuda_available = torch.cuda.is_available()
        gpu_name = torch.cuda.get_device_name(0) if cuda_available else None
        torch_version = torch.__version__
    else:
        cuda_available = False
        gpu_name = None
        torch_version = None

    return {
        "cuda_available": cuda_available,
        "gpu_name": gpu_name,
        "torch_version": torch_version,
        "python_version": sys.version.split()[0],
        "packages": {
            "elevenlabs": bool(config.ELEVENLABS_API_KEYS),
            "pycaps": _check_package("pycaps"),
            "ffmpeg": _check_ffmpeg(),
        },
        "env": {
            "elevenlabs_key_set": bool(config.ELEVENLABS_API_KEY),
            "gemini_keys_set": bool(config.GEMINI_API_KEYS),
            "deepl_key_set": bool(getattr(config, "DEEPL_API_KEY", "")),
        },
        # Always ElevenLabs — kept as a single-entry dict so existing UI
        # code that expects a model dropdown still works (it will simply
        # render one option).
        "stt_engines": {
            "elevenlabs": {
                "label": "ElevenLabs Speech-to-Text",
                "description": (
                    "Cloud-based STT — auto-translate via Gemini "
                    "to target language"
                ),
                "type": "elevenlabs",
            },
        },
    }


# ─── /api/elevenlabs/quota ───────────────────────────────────────────────────


@router.get("/api/elevenlabs/quota")
async def get_elevenlabs_quota() -> dict:
    """Fetch ElevenLabs subscription usage for every configured API key."""
    if not config.ELEVENLABS_API_KEYS:
        raise HTTPException(
            status_code=400, detail="No ELEVENLABS_API_KEY configured",
        )

    # httpx is lazy-imported so the rest of the API surface boots even
    # when httpx is missing — only this endpoint hard-requires it.
    import httpx

    async def _fetch_one(api_key: str, key_idx: int) -> dict:
        key_label = f"Key #{key_idx + 1}"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    "https://api.elevenlabs.io/v1/user/subscription",
                    headers={"xi-api-key": api_key},
                )
            if resp.status_code != 200:
                return {"key_label": key_label, "error": f"HTTP {resp.status_code}"}
            data = resp.json()
            return {
                "key_label": key_label,
                "character_count": data.get("character_count", 0),
                "character_limit": data.get("character_limit", 0),
                "tier": data.get("tier", "unknown"),
                "next_reset_unix": data.get("next_character_count_reset_unix", 0),
            }
        except httpx.RequestError as exc:
            return {"key_label": key_label, "error": str(exc)}

    results = await asyncio.gather(*[
        _fetch_one(key, idx)
        for idx, key in enumerate(config.ELEVENLABS_API_KEYS)
    ])
    return {"keys": list(results)}


# ─── /api/gemini/quota ───────────────────────────────────────────────────────


@router.get("/api/gemini/quota")
async def get_gemini_quota() -> dict:
    """Check Gemini API key validity for every configured key.

    The endpoint deliberately probes a cheap models-list call rather
    than counting tokens. Gemini's pricing-side counter is not exposed
    via the public API — the best we can offer the UI is a per-key
    health check (active / rate-limited / invalid / error).
    """
    if not config.GEMINI_API_KEYS:
        raise HTTPException(
            status_code=400, detail="No GEMINI_API_KEY configured",
        )

    import httpx

    async def _check_one(api_key: str, key_idx: int) -> dict:
        key_label = f"Key #{key_idx + 1}"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    "https://generativelanguage.googleapis.com/v1beta/models",
                    params={"key": api_key, "pageSize": 1},
                )
            if resp.status_code == 200:
                return {"key_label": key_label, "status": "active"}
            elif resp.status_code == 429:
                return {"key_label": key_label, "status": "rate_limited"}
            elif resp.status_code in (400, 403):
                return {"key_label": key_label, "status": "invalid"}
            else:
                return {
                    "key_label": key_label,
                    "status": "error",
                    "error": f"HTTP {resp.status_code}",
                }
        except httpx.RequestError as exc:
            return {"key_label": key_label, "status": "error", "error": str(exc)}

    results = await asyncio.gather(*[
        _check_one(key, idx)
        for idx, key in enumerate(config.GEMINI_API_KEYS)
    ])
    return {"keys": list(results)}


__all__ = ["router"]
