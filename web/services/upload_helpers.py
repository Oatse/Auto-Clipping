"""
web/services/upload_helpers.py — Streaming upload + filename sanitisation.

Pulled out of ``web/server.py`` so multiple route modules can share the
same upload sanitisation rules without re-importing FastAPI internals.

Two responsibilities:

  - ``safe_upload_name(filename)`` — strip path separators, collapse
    whitespace, whitelist characters so we never write anything outside
    the uploads dir or overflow Windows' MAX_PATH.
  - ``save_upload_streaming(upload, dest, ...)`` — stream the body to
    disk in fixed-size chunks and enforce ``MAX_UPLOAD_BYTES`` so a
    single client can't OOM the server with a multi-GB file.

The two helpers are pure: they take a FastAPI ``UploadFile`` and a
target ``Path``, and return a byte count. No global state.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import UploadFile

import config


class UploadTooLargeError(Exception):
    """Raised when a streaming upload would exceed ``MAX_UPLOAD_BYTES``.

    The partially-written destination file is left in place — the
    caller decides whether to unlink it or surface a detail in the
    HTTP error. This keeps the helper composable with multiple route
    handlers that may want different cleanup semantics.
    """


# ─── Filename sanitisation ───────────────────────────────────────────────────


def safe_upload_name(filename: str | None) -> str:
    """Sanitise an uploaded filename for safe filesystem use.

    Strips path separators, collapses whitespace, removes anything
    outside a conservative whitelist, and caps total length so the
    job_id prefix never pushes us over Windows' MAX_PATH when concat'd.

    Always returns a non-empty string — falls back to ``video.mp4``
    when the input is None / empty / fully scrubbed.
    """
    name = (filename or "video.mp4").strip()
    # Drop any directory components a malicious client may have included.
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    # Whitelist: alnum, dot, dash, underscore. Replace everything else.
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    # Avoid double-extensions and absurd lengths.
    if len(name) > 120:
        stem, _, ext = name.rpartition(".")
        name = (stem[: 120 - (len(ext) + 1)] + "." + ext) if ext else name[:120]
    return name or "video.mp4"


# ─── Streaming write ─────────────────────────────────────────────────────────


async def save_upload_streaming(
    upload: UploadFile,
    dest: Path,
    *,
    chunk_bytes: int | None = None,
    max_bytes: int | None = None,
) -> int:
    """Stream ``upload`` to ``dest`` and return the bytes written.

    The defaults pull from ``config.UPLOAD_CHUNK_BYTES`` and
    ``config.MAX_UPLOAD_BYTES`` so a single ``.env`` change tunes
    every upload route at once. Pass explicit values when a specific
    workspace needs tighter caps (e.g. an embed-only API surface).

    Raises ``UploadTooLargeError`` once the running total would exceed
    ``max_bytes``. The partially-written file is left in place — see
    the class docstring.
    """
    chunk_size: int = (
        chunk_bytes
        or getattr(config, "UPLOAD_CHUNK_BYTES", 1024 * 1024)
    )
    max_size: int = (
        max_bytes
        or getattr(config, "MAX_UPLOAD_BYTES", 4 * 1024 * 1024 * 1024)
    )

    written = 0
    try:
        with dest.open("wb") as out:
            while True:
                chunk = await upload.read(chunk_size)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_size:
                    raise UploadTooLargeError(
                        f"Upload exceeds limit of {max_size} bytes "
                        f"(received at least {written}). "
                        "Increase MAX_UPLOAD_BYTES in .env or "
                        "upload a smaller file."
                    )
                out.write(chunk)
    finally:
        await upload.close()
    return written


__all__ = [
    "UploadTooLargeError",
    "safe_upload_name",
    "save_upload_streaming",
]
