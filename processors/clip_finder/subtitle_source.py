"""
processors/clip_finder/subtitle_source.py — yt-dlp subtitle extraction.

Encapsulates the cookies-vs-no-cookies and player-client matrix needed
to keep working through YouTube's anti-bot churn. The interface promises
exactly one thing:

    SubtitleSource(cookies_file, cookies_browser).extract(url, output_dir, lang)
        → list[Segment] | None

Internally tries up to 16 attempts:
  4 strategies × 2 formats × {no-cookies, with-cookies}
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Callable

import yt_dlp
import yt_dlp.utils

from . import subtitle_parsers
from .transcript import Segment

LogFn = Callable[[str], None]


class _YtdlpLogger:
    """Forwards yt-dlp output to the caller's log function."""

    def __init__(self, log_fn: LogFn):
        self._log = log_fn

    def debug(self, msg: str) -> None:
        if not msg.startswith("[debug]"):
            self._log(f"  yt-dlp: {msg}")

    def warning(self, msg: str) -> None:
        self._log(f"  yt-dlp: {msg}")

    def error(self, msg: str) -> None:
        self._log(f"  yt-dlp: {msg}")


class SubtitleSource:
    """yt-dlp subtitle extractor — pure I/O, no LLM logic."""

    def __init__(self, cookies_file: str = "", cookies_browser: str = ""):
        self._cookies_file = cookies_file
        self._cookies_browser = cookies_browser

    # ── Public API ────────────────────────────────────────────────────────

    async def extract(
        self,
        url: str,
        output_dir: Path,
        lang: str = "en",
        log_fn: LogFn | None = None,
    ) -> list[Segment] | None:
        """Try every (strategy × format) combination, with cookie fallback."""
        output_dir.mkdir(parents=True, exist_ok=True)

        strategies = [
            (f"auto-subs ({lang})",
                {"writeautomaticsub": True, "subtitleslangs": [lang]}),
            (f"manual subs ({lang})",
                {"writesubtitles": True, "subtitleslangs": [lang]}),
            ("auto-subs (any language)",
                {"writeautomaticsub": True, "subtitleslangs": ["all", "-live_chat"]}),
            ("manual subs (any language)",
                {"writesubtitles": True, "subtitleslangs": ["all", "-live_chat"]}),
        ]
        formats = [("json3", {"subtitlesformat": "json3"}),
                   ("srt/vtt", {"subtitlesformat": "srt/vtt/best"})]

        # Pass 1: no cookies
        result = await self._try_passes(
            url, output_dir, strategies, formats, log_fn, with_cookies=False
        )
        if result is not None:
            return result

        # Pass 2: cookies
        if self._cookie_opts():
            if log_fn:
                log_fn("No subtitles without auth — retrying all strategies with cookies...")
            return await self._try_passes(
                url, output_dir, strategies, formats, log_fn, with_cookies=True
            )

        if log_fn:
            log_fn("No subtitles available from YouTube (tried all strategies)")
        return None

    # ── Pass orchestration ───────────────────────────────────────────────

    async def _try_passes(
        self,
        url: str,
        output_dir: Path,
        strategies: list[tuple[str, dict]],
        formats: list[tuple[str, dict]],
        log_fn: LogFn | None,
        with_cookies: bool,
    ) -> list[Segment] | None:
        cookie_opts = self._cookie_opts() if with_cookies else None
        suffix = " (with cookies)" if with_cookies else ""

        for label, base_opts in strategies:
            for fmt_label, fmt_opts in formats:
                if log_fn:
                    log_fn(f"Trying {label} [{fmt_label}]{suffix}...")
                segments = await self._run_attempt(
                    url=url,
                    output_dir=output_dir,
                    extra_opts={**base_opts, **fmt_opts},
                    log_fn=log_fn,
                    cookie_opts=cookie_opts,
                )
                if segments is not None:
                    if log_fn:
                        log_fn(f"Found subtitles via {label} [{fmt_label}]{suffix}: {len(segments)} segments")
                    return segments
        return None

    async def _run_attempt(
        self,
        url: str,
        output_dir: Path,
        extra_opts: dict,
        log_fn: LogFn | None,
        cookie_opts: dict | None,
    ) -> list[Segment] | None:
        # Wipe stale subtitle files so format-detection picks the right one
        for old in output_dir.glob("subs*"):
            old.unlink(missing_ok=True)

        sub_file = str(output_dir / "subs")
        ydl_opts = {
            **self._base_opts(log_fn),
            **(cookie_opts or {}),
            **extra_opts,
            "skip_download": True,
            "ignore_no_formats_error": True,
            "outtmpl": sub_file,
        }

        def _run() -> None:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                try:
                    ydl.download([url])
                except yt_dlp.utils.DownloadError:
                    pass

        await asyncio.to_thread(_run)

        # Order matters: prefer json3 (richer) over srt/vtt
        for ext, parser in (
            ("json3", subtitle_parsers.parse_json3),
            ("srt", subtitle_parsers.parse_srt),
            ("vtt", subtitle_parsers.parse_vtt),
        ):
            for p in output_dir.glob(f"subs*.{ext}"):
                if "live_chat" in p.name:
                    continue
                return parser(p)

        return None

    # ── yt-dlp option builders ───────────────────────────────────────────

    def _base_opts(self, log_fn: LogFn | None) -> dict:
        opts: dict = {
            "quiet": True,
            "no_warnings": False,
            "extractor_args": {
                "youtube": {"player_client": ["default", "android_vr"]},
            },
        }
        if log_fn:
            opts["logger"] = _YtdlpLogger(log_fn)
        return opts

    def _cookie_opts(self) -> dict:
        """Cookie-aware opts. tv_downgraded → tv → web_creator chain."""
        opts: dict = {}
        if self._cookies_file:
            opts["cookiefile"] = self._cookies_file
        elif self._cookies_browser:
            opts["cookiesfrombrowser"] = (self._cookies_browser,)
        else:
            return {}

        opts["extractor_args"] = {
            "youtube": {"player_client": ["tv_downgraded", "tv", "web_creator"]}
        }
        opts["js_runtimes"] = {"node": {}, "deno": {}}
        return opts


__all__ = ["SubtitleSource"]
