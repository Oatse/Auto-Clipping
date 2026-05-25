"""
web.services.all_in.stages.source — Full source video download.

The All In Workspace downloads the entire source video once
(per design grilling Q3) so that:

- Speaker diarization runs on continuous audio (stable speaker IDs
  across all Clips from one Job).
- Cuts are frame-accurate from a single input vs. yt-dlp's
  ``--download-sections`` keyframe alignment.
- Per-Clip retry stays cheap — the source persists with the Job
  (Q12) and retry re-enters the per-clip loop without re-downloading.

This stage wraps yt-dlp with the same cookie-fallback pattern used
by ``processors.clip_finder.downloader.ClipDownloader``, but caps
quality at 720p to keep disk usage bounded (~2–3 GB for a 4-hour
livestream).

Public API:
    download_source(url, output_dir, *, cookies_file, cookies_browser,
                    log_fn) -> SourceVideo
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import yt_dlp
import yt_dlp.utils


LogFn = Callable[[str], None]


@dataclass(frozen=True)
class SourceVideo:
    """Result of a successful source download.

    ``path`` is the local MP4 file (single file, audio + video muxed).
    ``title`` is the YouTube-reported title — surfaced on the Job
    list and the All In hero card.  ``duration_seconds`` lets the
    runner pick a sane default for ``start_offset`` validation.
    """

    path: Path
    title: str
    duration_seconds: float


class SourceDownloadError(RuntimeError):
    """Raised when yt-dlp can neither download nor probe the source."""


# ─── Public entry point ──────────────────────────────────────────────────────

async def download_source(
    *,
    url: str,
    output_dir: Path,
    cookies_file: str = "",
    cookies_browser: str = "",
    max_height: int = 720,
    log_fn: LogFn | None = None,
) -> SourceVideo:
    """Download the full YouTube source video as a single MP4.

    Capped at ``max_height`` (default 720p) so a 4-hour livestream
    stays around 2–3 GB on disk.  4K masters are wasted bytes for
    short-form output that ends up at 1080p or below.

    Tries without cookies first; if yt-dlp returns a DownloadError
    that smells like an age/region/login wall, retries with the
    configured cookie source (cookies.txt or browser).

    Returns a :class:`SourceVideo` with the local path, video title,
    and duration.  Raises :class:`SourceDownloadError` if both the
    no-cookie and cookie attempts fail.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / "source.mp4"

    common_opts = _build_common_opts(output_file, max_height)

    # First attempt — no cookies.
    err: list[str] = []
    primary_opts = {**_base_opts(log_fn), **common_opts}
    if log_fn:
        log_fn(f"Downloading source video (max {max_height}p)...")
    info = await _run_yt_dlp(primary_opts, url, err)

    # Cookie fallback — only if a cookie source is configured AND the
    # first attempt failed.  Mirrors ClipDownloader's policy.
    if (err or info is None) and (cookies_file or cookies_browser):
        if log_fn:
            log_fn("First attempt failed, retrying with cookies...")
        err.clear()
        cookie_opts = {
            **_base_opts(log_fn),
            **_cookie_opts(cookies_file, cookies_browser),
            **common_opts,
        }
        info = await _run_yt_dlp(cookie_opts, url, err)

    if err or info is None:
        message = err[0][:300] if err else "yt-dlp returned no info"
        raise SourceDownloadError(f"Failed to download source: {message}")

    resolved = _resolve_output(output_file, output_dir)
    if resolved is None:
        raise SourceDownloadError(
            "yt-dlp reported success but no source file landed on disk"
        )

    title = (info.get("title") or "untitled").strip()
    duration = float(info.get("duration") or 0.0)

    if log_fn:
        size_mb = resolved.stat().st_size / (1024 * 1024)
        log_fn(f"Source downloaded: {title} ({size_mb:.1f} MB, {duration:.0f}s)")

    return SourceVideo(path=resolved, title=title, duration_seconds=duration)


# ─── yt-dlp option builders ──────────────────────────────────────────────────

def _build_common_opts(output_file: Path, max_height: int) -> dict:
    """Per-download options shared across primary + cookie-fallback runs.

    Caps at ``max_height`` and prefers MP4+M4A so the muxed output
    needs no transcoding pass.  ``noplaylist`` keeps a single-video
    download from accidentally pulling an entire channel when given
    a ``/watch?...&list=...`` URL.
    """
    format_chain = (
        f"bestvideo[height<={max_height}][ext=mp4]+bestaudio[ext=m4a]/"
        f"bestvideo[height<={max_height}]+bestaudio/"
        f"best[height<={max_height}]/best"
    )
    return {
        "concurrent_fragment_downloads": 4,
        "format": format_chain,
        "format_sort": ["res", "fps", "vcodec:h264", "acodec:m4a"],
        "merge_output_format": "mp4",
        "noplaylist": True,
        "outtmpl": str(output_file),
        "socket_timeout": 60,
        "retries": 10,
        "fragment_retries": 10,
        "file_access_retries": 5,
        "retry_sleep_functions": {"http": lambda n: 2 ** n},
    }


def _base_opts(log_fn: LogFn | None) -> dict:
    """Logger + extractor settings shared with ClipDownloader."""
    opts: dict = {
        "quiet": True,
        "no_warnings": False,
        "extractor_args": {
            "youtube": {"player_client": ["default", "android_vr"]},
        },
    }
    if log_fn:
        # Reuse the same logger adapter so log output is consistent
        # with what users already see in the Clip Finder workspace.
        from processors.clip_finder.subtitle_source import _YtdlpLogger
        opts["logger"] = _YtdlpLogger(log_fn)
    return opts


def _cookie_opts(cookies_file: str, cookies_browser: str) -> dict:
    """Cookie-aware overrides — only applied on the fallback path."""
    opts: dict = {}
    if cookies_file:
        opts["cookiefile"] = cookies_file
    elif cookies_browser:
        opts["cookiesfrombrowser"] = (cookies_browser,)
    else:
        return {}

    # Same multi-client list as ClipDownloader for SABR-bypass parity.
    opts["extractor_args"] = {
        "youtube": {
            "player_client": [
                "default",
                "android_vr",
                "mweb",
                "ios",
                "tv",
                "tv_downgraded",
                "web_creator",
            ],
        },
    }
    opts["js_runtimes"] = {"node": {}, "deno": {}}
    return opts


# ─── runner / file resolution ────────────────────────────────────────────────

async def _run_yt_dlp(opts: dict, url: str, err_holder: list[str]) -> dict | None:
    """Run yt-dlp on a worker thread, capturing errors and info dict."""
    info_holder: dict[str, dict | None] = {"info": None}

    def _run() -> None:
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                # extract_info(download=True) does both probe + download
                # in a single pass, so we get the title/duration metadata
                # back without a second network round-trip.
                info = ydl.extract_info(url, download=True)
                if info is not None:
                    info_holder["info"] = info
        except yt_dlp.utils.DownloadError as exc:
            err_holder.append(str(exc))
        except Exception as exc:  # noqa: BLE001 — surface anything yt-dlp raises
            err_holder.append(f"{type(exc).__name__}: {exc}")

    await asyncio.to_thread(_run)
    return info_holder["info"]


def _resolve_output(expected: Path, output_dir: Path) -> Path | None:
    """Find the muxed output file even if yt-dlp picked a different ext."""
    if expected.exists():
        return expected
    # yt-dlp may land the file at source.webm / source.mkv if the format
    # chain falls through to a non-MP4 source.  Pick whatever sits next
    # to the expected path with a video extension.
    stem = expected.stem
    for found in output_dir.glob(f"{stem}.*"):
        if found.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"}:
            return found
    return None


__all__ = ["SourceVideo", "SourceDownloadError", "download_source"]
