"""
processors/clip_finder/audio_signals.py — Audio energy event extraction.

Lightweight peak / silence detector built on top of FFmpeg. No heavy
dependencies (no librosa / scipy) — works with whatever the rest of the
project already needs.

Pipeline:
  1. Download audio-only (m4a) via yt-dlp into output_dir/audio.m4a
  2. Run FFmpeg `astats` filter at 1-second granularity → parse RMS dB
  3. Detect peaks (sustained jump above local baseline) → SignalEvent
  4. Detect silence runs (silencedetect filter) → SignalEvent

If ffmpeg / ffprobe / yt-dlp aren't available, returns [] gracefully.
"""

from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path
from typing import Callable

import yt_dlp
import yt_dlp.utils

from models.clip import SignalEvent, SignalKind

LogFn = Callable[[str], None]


# ─── Configuration ────────────────────────────────────────────────────────────

PEAK_DB_THRESHOLD = 6.0    # peak ≥ baseline + 6 dB → flag as audio_peak
SILENCE_NOISE_DB = -38.0    # below -38 dB counts as silence
SILENCE_MIN_DURATION = 5.0  # silence run ≥ 5 s


class AudioSignalExtractor:
    """Extracts audio peaks + silence runs from a YouTube URL."""

    def __init__(
        self,
        ffmpeg_path: str = "ffmpeg",
        ffprobe_path: str = "ffprobe",
        cookies_file: str = "",
        cookies_browser: str = "",
    ):
        self._ffmpeg = ffmpeg_path
        self._ffprobe = ffprobe_path
        self._cookies_file = cookies_file
        self._cookies_browser = cookies_browser

    async def extract(
        self,
        url: str,
        output_dir: Path,
        log_fn: LogFn | None = None,
    ) -> list[SignalEvent]:
        """Return list of SignalEvent for audio peaks + silence runs."""
        if not shutil.which(self._ffmpeg):
            if log_fn:
                log_fn("AudioSignals: ffmpeg not available, skipping")
            return []

        output_dir.mkdir(parents=True, exist_ok=True)
        audio_path = output_dir / "audio.m4a"

        if not audio_path.exists():
            ok = await self._download_audio(url, audio_path, log_fn)
            if not ok:
                return []

        events: list[SignalEvent] = []
        try:
            silences = await self._detect_silences(audio_path, log_fn)
            events.extend(silences)
        except Exception as exc:
            if log_fn:
                log_fn(f"AudioSignals: silence detection failed: {exc}")

        try:
            peaks = await self._detect_peaks(audio_path, log_fn)
            events.extend(peaks)
        except Exception as exc:
            if log_fn:
                log_fn(f"AudioSignals: peak detection failed: {exc}")

        events.sort(key=lambda e: e.start)
        if log_fn:
            n_peaks = sum(1 for e in events if e.kind == SignalKind.AUDIO_PEAK)
            n_sil = sum(1 for e in events if e.kind == SignalKind.AUDIO_SILENCE)
            log_fn(f"AudioSignals: {n_peaks} peaks, {n_sil} silences detected")
        return events

    # ── audio download ──────────────────────────────────────────────────

    async def _download_audio(
        self, url: str, output_path: Path, log_fn: LogFn | None
    ) -> bool:
        if log_fn:
            log_fn("AudioSignals: downloading audio for energy analysis...")

        ydl_opts: dict = {
            "format": "bestaudio[ext=m4a]/bestaudio",
            "outtmpl": str(output_path),
            "quiet": True,
            "noplaylist": True,
            "extractor_args": {
                "youtube": {"player_client": ["default", "android_vr"]},
            },
        }
        if self._cookies_file:
            ydl_opts["cookiefile"] = self._cookies_file
            ydl_opts["extractor_args"] = {
                "youtube": {"player_client": ["tv_downgraded", "tv", "web_creator"]}
            }
            ydl_opts["js_runtimes"] = {"node": {}, "deno": {}}
        elif self._cookies_browser:
            ydl_opts["cookiesfrombrowser"] = (self._cookies_browser,)
            ydl_opts["extractor_args"] = {
                "youtube": {"player_client": ["tv_downgraded", "tv", "web_creator"]}
            }
            ydl_opts["js_runtimes"] = {"node": {}, "deno": {}}

        err: list[str] = []

        def _run() -> None:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                try:
                    ydl.download([url])
                except yt_dlp.utils.DownloadError as exc:
                    err.append(str(exc))

        await asyncio.to_thread(_run)

        if err or not output_path.exists():
            if log_fn:
                log_fn(f"AudioSignals: audio download failed: {err[0][:200] if err else 'no file'}")
            return False
        return True

    # ── silence detection ────────────────────────────────────────────────

    async def _detect_silences(
        self, audio_path: Path, log_fn: LogFn | None
    ) -> list[SignalEvent]:
        cmd = [
            self._ffmpeg,
            "-hide_banner",
            "-nostats",
            "-i", str(audio_path),
            "-af",
            f"silencedetect=noise={SILENCE_NOISE_DB}dB:d={SILENCE_MIN_DURATION}",
            "-f", "null",
            "-",
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        text = stderr.decode("utf-8", errors="replace")

        events: list[SignalEvent] = []
        starts: list[float] = []
        for line in text.splitlines():
            m = re.search(r"silence_start:\s*([\d.]+)", line)
            if m:
                starts.append(float(m.group(1)))
                continue
            m = re.search(
                r"silence_end:\s*([\d.]+)\s*\|\s*silence_duration:\s*([\d.]+)",
                line,
            )
            if m:
                end_t = float(m.group(1))
                dur = float(m.group(2))
                start_t = end_t - dur
                events.append(
                    SignalEvent(
                        kind=SignalKind.AUDIO_SILENCE,
                        start=round(start_t, 2),
                        end=round(end_t, 2),
                        intensity=min(1.0, dur / 30.0),
                        label=f"silence {dur:.1f}s",
                    )
                )
        return events

    # ── peak detection (1s windows via astats) ───────────────────────────

    async def _detect_peaks(
        self, audio_path: Path, log_fn: LogFn | None
    ) -> list[SignalEvent]:
        # Use astats with metadata to get RMS dB per 1-second window
        cmd = [
            self._ffmpeg,
            "-hide_banner",
            "-nostats",
            "-i", str(audio_path),
            "-af",
            "asetnsamples=n=44100,astats=metadata=1:reset=1,"
            "ametadata=mode=print:key=lavfi.astats.Overall.RMS_level:"
            f"file=-",
            "-f", "null",
            "-",
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        text = stdout.decode("utf-8", errors="replace")

        # Parse pairs of "frame:N pts:T pts_time:Sec\nlavfi.astats...RMS_level=DB"
        time_re = re.compile(r"pts_time:([\d.]+)")
        rms_re = re.compile(r"RMS_level=(-?\d+(?:\.\d+)?|-?inf)")
        samples: list[tuple[float, float]] = []
        cur_t: float | None = None
        for line in text.splitlines():
            m = time_re.search(line)
            if m:
                cur_t = float(m.group(1))
                continue
            m = rms_re.search(line)
            if m and cur_t is not None:
                raw = m.group(1)
                if raw.endswith("inf"):
                    db = -120.0
                else:
                    db = float(raw)
                samples.append((cur_t, db))
                cur_t = None

        if len(samples) < 5:
            return []

        # Compute rolling baseline (median of 30s window) and flag peaks
        events: list[SignalEvent] = []
        window = 30
        n = len(samples)
        # Pre-extract dB list for percentile computation
        dbs = [s[1] for s in samples]

        i = 0
        while i < n:
            t, db = samples[i]
            lo = max(0, i - window)
            hi = min(n, i + window)
            window_dbs = sorted(dbs[lo:hi])
            mid = window_dbs[len(window_dbs) // 2]
            if db - mid >= PEAK_DB_THRESHOLD and db > -25.0:
                # Cluster contiguous peak frames into one event
                end_idx = i
                while end_idx + 1 < n and samples[end_idx + 1][1] - mid >= PEAK_DB_THRESHOLD:
                    end_idx += 1
                end_t = samples[end_idx][0] + 1.0
                peak_intensity = min(1.0, (db - mid) / 20.0)
                events.append(
                    SignalEvent(
                        kind=SignalKind.AUDIO_PEAK,
                        start=round(t, 2),
                        end=round(end_t, 2),
                        intensity=round(peak_intensity, 3),
                        label=f"+{db - mid:.1f} dB above baseline",
                    )
                )
                i = end_idx + 1
                continue
            i += 1
        return events


__all__ = ["AudioSignalExtractor"]
