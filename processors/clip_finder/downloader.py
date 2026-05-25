"""
processors/clip_finder/downloader.py — Per-clip yt-dlp section downloader.

Encapsulates the cookie-fallback + NVENC re-encode + filename sanitisation
logic that used to live inside ClipFinder.download_clip_sections().

Public API:
    ClipDownloader(cookies_file, cookies_browser).download(
        url, clips, output_dir, log_fn=None, index_offset=0
    ) → list[Path]

The downloader is "sticky": once a single clip needs cookies on a given
URL, every subsequent clip skips the no-cookie attempt to save time.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Callable, Sequence

import yt_dlp
import yt_dlp.utils

from .heuristics import fmt_duration, fmt_time

LogFn = Callable[[str], None]


class ClipDownloader:
    """Downloads only the requested time ranges from a YouTube URL."""

    def __init__(
        self,
        cookies_file: str = "",
        cookies_browser: str = "",
        nvenc_preset: str = "p4",
        nvenc_cq: str = "23",
    ):
        self._cookies_file = cookies_file
        self._cookies_browser = cookies_browser
        self._nvenc_preset = nvenc_preset
        self._nvenc_cq = nvenc_cq

    async def download(
        self,
        *,
        url: str,
        clips: Sequence[dict],
        output_dir: Path,
        log_fn: LogFn | None = None,
        index_offset: int = 0,
    ) -> list[Path]:
        """Download each clip's section, return the resulting MP4 paths."""
        output_dir.mkdir(parents=True, exist_ok=True)
        clip_paths: list[Path] = []
        use_cookies_first = False

        for raw_idx, clip in enumerate(clips):
            i = raw_idx + index_offset
            start = float(clip["start"])
            end = float(clip["end"])
            title = clip.get("title", f"clip_{i}")

            safe_title = re.sub(r"[^\w\s-]", "", title)[:40].strip().replace(" ", "_")
            if not safe_title:
                safe_title = f"clip_{i}"
            output_file = output_dir / f"clip_{i + 1:03d}_{safe_title}.mp4"

            if log_fn:
                log_fn(
                    f"Downloading clip {i + 1}/{len(clips) + index_offset}: "
                    f"{fmt_time(start)} - {fmt_time(end)} \"{title}\""
                )

            common_opts = self._build_common_opts(start, end, output_file)
            err: list[str] = []

            primary_opts = {
                **self._base_opts(log_fn),
                **(self._cookie_opts() if use_cookies_first else {}),
                **common_opts,
            }
            await self._run_yt_dlp(primary_opts, url, err)

            if err and not use_cookies_first and self._cookie_opts():
                if log_fn:
                    log_fn(f"Clip {i + 1}: first attempt failed, retrying with cookies...")
                fallback_opts = {
                    **self._base_opts(log_fn),
                    **self._cookie_opts(),
                    **common_opts,
                }
                err.clear()
                await self._run_yt_dlp(fallback_opts, url, err)
                if not err:
                    use_cookies_first = True

            if err:
                if log_fn:
                    log_fn(f"Warning: Failed to download clip {i + 1}: {err[0][:200]}")
                continue

            resolved = self._resolve_output(output_file, output_dir, i + 1, safe_title)
            if resolved is None:
                if log_fn:
                    log_fn(f"Warning: Clip {i + 1} file not found after download")
                continue
            clip_paths.append(resolved)
            if log_fn:
                log_fn(f"Clip {i + 1} saved ({fmt_duration(end - start)})")

        return clip_paths

    # ── option builders ──────────────────────────────────────────────────

    def _build_common_opts(self, start: float, end: float, output_file: Path) -> dict:
        ffmpeg_args = [
            "-c:v", "h264_nvenc",
            "-preset", self._nvenc_preset,
            "-cq", self._nvenc_cq,
        ]
        # Explicit height preference so we never silently settle for 360p
        # when a degraded player_client (e.g. TV clients in the cookie
        # fallback path) only advertises low-resolution formats.
        format_chain = (
            "bestvideo[height>=1080][ext=mp4]+bestaudio[ext=m4a]/"
            "bestvideo[height>=720][ext=mp4]+bestaudio[ext=m4a]/"
            "bestvideo[height>=480][ext=mp4]+bestaudio[ext=m4a]/"
            "bestvideo[ext=mp4]+bestaudio[ext=m4a]/"
            "bestvideo+bestaudio/best"
        )
        return {
            "concurrent_fragment_downloads": 4,
            "download_ranges": yt_dlp.utils.download_range_func(None, [(start, end)]),
            "format": format_chain,
            "format_sort": ["res", "fps", "vcodec:h264", "acodec:m4a"],
            "merge_output_format": "mp4",
            "noplaylist": True,
            "outtmpl": str(output_file),
            "force_keyframes_at_cuts": True,
            "socket_timeout": 60,
            "retries": 10,
            "fragment_retries": 10,
            "file_access_retries": 5,
            "retry_sleep_functions": {"http": lambda n: 2 ** n},
            "external_downloader_args": {"ffmpeg": ffmpeg_args},
            "postprocessor_args": {"ffmpeg": ffmpeg_args},
        }

    def _base_opts(self, log_fn: LogFn | None) -> dict:
        opts: dict = {
            "quiet": True,
            "no_warnings": False,
            "extractor_args": {
                "youtube": {"player_client": ["default", "android_vr"]},
            },
        }
        if log_fn:
            from .subtitle_source import _YtdlpLogger  # share logger adapter
            opts["logger"] = _YtdlpLogger(log_fn)
        return opts

    def _cookie_opts(self) -> dict:
        opts: dict = {}
        if self._cookies_file:
            opts["cookiefile"] = self._cookies_file
        elif self._cookies_browser:
            opts["cookiesfrombrowser"] = (self._cookies_browser,)
        else:
            return {}
        # Order matters: high-res clients first so 1080p+ formats are
        # advertised. The TV/creator clients are kept last as a SABR-bypass
        # safety net but on their own they only expose ≤360–720p, which is
        # what caused subsequent clips to drop to 640×360.
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
                ]
            }
        }
        opts["js_runtimes"] = {"node": {}, "deno": {}}
        return opts

    # ── runner / file resolution ─────────────────────────────────────────

    @staticmethod
    async def _run_yt_dlp(opts: dict, url: str, err_holder: list[str]) -> None:
        def _run() -> None:
            with yt_dlp.YoutubeDL(opts) as ydl:
                try:
                    ydl.download([url])
                except yt_dlp.utils.DownloadError as exc:
                    err_holder.append(str(exc))

        await asyncio.to_thread(_run)

    @staticmethod
    def _resolve_output(
        expected: Path, output_dir: Path, idx: int, safe_title: str
    ) -> Path | None:
        if expected.exists():
            return expected
        for found in output_dir.glob(f"clip_{idx:03d}_{safe_title}*"):
            return found
        return None


__all__ = ["ClipDownloader"]
