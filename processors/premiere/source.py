"""
processors/premiere/source.py — Master media acquisition + probing.

Compilation mode needs two different things from one URL:

  * a **master video** the Premiere timeline references (downloaded once,
    never cut), and
  * **audio only**, which is all the moment analysis actually needs and
    which arrives far sooner than a two-hour video.

Splitting them lets analysis start while the video is still downloading.

``probe_media`` reads the exact frame rate, which FCPXML generation
depends on: a timeline built at the wrong rate drifts every clip.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Callable

import yt_dlp
import yt_dlp.utils

LogFn = Callable[[str], None]


# ─── Media metadata ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MediaInfo:
    """Facts about a media file that a timeline must agree with.

    ``fps`` is kept as an exact :class:`Fraction` because broadcast rates
    are rational (30000/1001, not 29.97). Rounding here is what makes a
    long timeline drift, so the rounding decision is deferred to
    :mod:`processors.premiere.fcpxml`.
    """

    path: Path
    fps: Fraction
    width: int
    height: int
    duration: float

    @property
    def fps_float(self) -> float:
        return float(self.fps)

    @property
    def is_ntsc(self) -> bool:
        """True for the /1001 broadcast rates (23.976, 29.97, 59.94).

        FCP7 XML encodes these as an integer ``timebase`` plus an
        ``ntsc`` flag rather than a fractional rate.
        """
        return self.fps.denominator % 1001 == 0 or (
            self.fps.denominator != 1 and abs(float(self.fps) - round(float(self.fps))) > 1e-6
        )

    @property
    def timebase(self) -> int:
        """Integer timebase Premiere expects (24, 25, 30, 50, 60...)."""
        return int(round(float(self.fps)))


def probe_media(
    path: Path,
    *,
    ffprobe_path: str | None = None,
    log_fn: LogFn | None = None,
) -> MediaInfo:
    """Return :class:`MediaInfo` for ``path`` using ffprobe.

    Falls back to 30 fps / 1920x1080 only when ffprobe cannot be reached,
    and says so loudly — a silently wrong frame rate would misplace every
    clip in the generated timeline.
    """
    if ffprobe_path is None:
        try:
            import config as _config

            ffprobe_path = getattr(_config, "FFPROBE_PATH", "ffprobe")
        except Exception:  # noqa: BLE001 — config problems must not break probing
            ffprobe_path = "ffprobe"

    cmd = [
        ffprobe_path,
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=r_frame_rate,avg_frame_rate,width,height:format=duration",
        "-of", "json",
        str(path),
    ]
    try:
        completed = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60, check=False,
        )
        data = json.loads(completed.stdout or "{}")
        stream = (data.get("streams") or [{}])[0]
        fmt = data.get("format") or {}

        fps = _parse_rate(stream.get("r_frame_rate")) or _parse_rate(
            stream.get("avg_frame_rate")
        )
        if fps is None:
            raise ValueError(f"no usable frame rate in ffprobe output: {completed.stderr[:200]}")

        return MediaInfo(
            path=path,
            fps=fps,
            width=int(stream.get("width") or 1920),
            height=int(stream.get("height") or 1080),
            duration=float(fmt.get("duration") or 0.0),
        )
    except Exception as exc:  # noqa: BLE001 — degrade with a loud warning
        if log_fn:
            log_fn(
                f"WARNING: ffprobe failed on {path.name} ({exc}). Falling back to "
                "30 fps / 1920x1080 — the generated timeline may be misaligned. "
                "Set FFPROBE_PATH if ffprobe is not on PATH."
            )
        return MediaInfo(
            path=path, fps=Fraction(30, 1), width=1920, height=1080, duration=0.0,
        )


def _parse_rate(raw: object) -> Fraction | None:
    """Parse ffprobe's ``num/den`` rate string into an exact Fraction."""
    if not raw:
        return None
    text = str(raw).strip()
    if text in ("0/0", "N/A", ""):
        return None
    try:
        value = Fraction(text)
    except (ValueError, ZeroDivisionError):
        return None
    return value if value > 0 else None


# ─── Acquisition ─────────────────────────────────────────────────────────────


class MasterSource:
    """Downloads the whole VOD (video) and/or its audio track."""

    def __init__(self, cookies_file: str = "", cookies_browser: str = ""):
        self._cookies_file = cookies_file
        self._cookies_browser = cookies_browser

    async def download_master(
        self,
        *,
        url: str,
        output_dir: Path,
        max_height: int = 1080,
        log_fn: LogFn | None = None,
    ) -> Path | None:
        """Download the full VOD once. Returns the file path, or None."""
        output_dir.mkdir(parents=True, exist_ok=True)
        target = output_dir / "master.%(ext)s"
        opts = {
            **self._base_opts(log_fn),
            "format": (
                f"bestvideo[height<={max_height}][ext=mp4]+bestaudio[ext=m4a]/"
                f"bestvideo[height<={max_height}]+bestaudio/"
                f"best[height<={max_height}]/best"
            ),
            "format_sort": ["res", "fps", "vcodec:h264", "acodec:m4a"],
            "merge_output_format": "mp4",
            "outtmpl": str(target),
            "noplaylist": True,
            "concurrent_fragment_downloads": 4,
            "socket_timeout": 60,
            "retries": 10,
            "fragment_retries": 10,
        }
        if log_fn:
            log_fn("Downloading master VOD (full length, referenced by the timeline)...")
        return await self._download(url, opts, output_dir, "master", log_fn)

    async def download_audio(
        self,
        *,
        url: str,
        output_dir: Path,
        log_fn: LogFn | None = None,
    ) -> Path | None:
        """Download audio only — enough for analysis, and far faster."""
        output_dir.mkdir(parents=True, exist_ok=True)
        target = output_dir / "master_audio.%(ext)s"
        opts = {
            **self._base_opts(log_fn),
            "format": "bestaudio[ext=m4a]/bestaudio/best",
            "outtmpl": str(target),
            "noplaylist": True,
            "socket_timeout": 60,
            "retries": 10,
            "fragment_retries": 10,
        }
        if log_fn:
            log_fn("Downloading audio track for analysis...")
        return await self._download(url, opts, output_dir, "master_audio", log_fn)

    # ── internals ────────────────────────────────────────────────────────

    async def _download(
        self,
        url: str,
        opts: dict,
        output_dir: Path,
        stem: str,
        log_fn: LogFn | None,
    ) -> Path | None:
        """Run yt-dlp, retrying once with cookies before giving up."""
        err: list[str] = []
        await self._run(opts, url, err)
        found = self._resolve(output_dir, stem)
        if found:
            return found

        cookie_opts = self._cookie_opts()
        if cookie_opts:
            if log_fn:
                log_fn("Retrying download with cookies...")
            err.clear()
            await self._run({**opts, **cookie_opts}, url, err)
            found = self._resolve(output_dir, stem)
            if found:
                return found

        if log_fn:
            detail = f" ({err[0][:200]})" if err else ""
            log_fn(f"ERROR: download failed for {stem}{detail}")
        return None

    @staticmethod
    async def _run(opts: dict, url: str, err_holder: list[str]) -> None:
        def _go() -> None:
            with yt_dlp.YoutubeDL(opts) as ydl:
                try:
                    ydl.download([url])
                except yt_dlp.utils.DownloadError as exc:
                    err_holder.append(str(exc))

        await asyncio.to_thread(_go)

    @staticmethod
    def _resolve(output_dir: Path, stem: str) -> Path | None:
        """Find the produced file — the extension depends on the format."""
        for candidate in sorted(output_dir.glob(f"{stem}.*")):
            if candidate.is_file() and candidate.stat().st_size > 0:
                return candidate
        return None

    def _base_opts(self, log_fn: LogFn | None) -> dict:
        # Mirrors ClipDownloader: listing several player clients is what
        # gets HD adaptive formats advertised, and js_runtimes enables
        # nsig deciphering so the web client returns HD URLs.
        opts: dict = {
            "quiet": True,
            "no_warnings": False,
            "extractor_args": {
                "youtube": {
                    "player_client": [
                        "default", "android_vr", "mweb", "ios",
                        "tv", "tv_downgraded", "web_creator",
                    ],
                },
            },
            "js_runtimes": {"node": {}, "deno": {}},
        }
        if log_fn:
            from processors.clip_finder.subtitle_source import _YtdlpLogger

            opts["logger"] = _YtdlpLogger(log_fn)
        return opts

    def _cookie_opts(self) -> dict:
        if self._cookies_file:
            return {"cookiefile": self._cookies_file}
        if self._cookies_browser:
            return {"cookiesfrombrowser": (self._cookies_browser,)}
        return {}


__all__ = ["MediaInfo", "probe_media", "MasterSource"]
