"""
processors/clip_finder.py — YouTube Clip Finder using yt-dlp + Gemini AI.

Flow (2-phase):
  Phase 1 (auto — on "Find Clips"):
    1. yt-dlp --skip-download → extract transcript via write-auto-sub / write-sub
       (tries: auto-subs in lang → manual subs in lang → auto-subs any lang → manual subs any lang)
    2. Gemini AI analyzes transcript → returns list of clips
    3. Results displayed to user

  Phase 2 (on-demand — user clicks "Download Clips"):
    4. yt-dlp --download-sections → download only the relevant sections
    5. Clips ready for preview / download
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from pathlib import Path
from typing import Callable

import httpx
from loguru import logger

import config


class ClipFinderError(RuntimeError):
    """Raised when a clip finder operation fails."""
    pass


class ClipFinder:
    """Orchestrates transcript extraction, AI analysis, and selective clip download."""

    def __init__(self):
        self.ytdlp_path = self._resolve_ytdlp()
        self._cookies_file = getattr(config, "YTDLP_COOKIES_FILE", "")
        self._cookies_browser = getattr(config, "YTDLP_COOKIES_BROWSER", "")

    def _base_ytdlp_cmd(self) -> list[str]:
        """Return common yt-dlp flags (js-runtime, cookies)."""
        cmd = [self.ytdlp_path, "--js-runtimes", "node"]
        if self._cookies_file and Path(self._cookies_file).is_file():
            cmd += ["--cookies", self._cookies_file]
        elif self._cookies_browser:
            cmd += ["--cookies-from-browser", self._cookies_browser]
        return cmd

    # ── Resolve yt-dlp binary ────────────────────────────────────────────────

    @staticmethod
    def _resolve_ytdlp() -> str:
        """Find yt-dlp binary from config, ./bin/, or system PATH."""
        env_path = getattr(config, "YTDLP_PATH", "")
        if env_path and Path(env_path).is_file():
            return str(Path(env_path).resolve())

        bin_path = config.BASE_DIR / "bin" / "yt-dlp.exe"
        if bin_path.is_file():
            return str(bin_path)

        which = shutil.which("yt-dlp")
        if which:
            return which

        raise EnvironmentError(
            "yt-dlp not found. Place yt-dlp.exe in ./bin/ or set YTDLP_PATH in .env"
        )

    # ── 1. Extract Transcript (no video download) ────────────────────────────

    async def extract_subtitles(
        self,
        url: str,
        output_dir: Path,
        lang: str = "en",
        log_fn: Callable[[str], None] | None = None,
    ) -> list[dict] | None:
        """
        Extract subtitles from YouTube using yt-dlp (no video download).

        Tries multiple strategies in order:
          1. Auto-generated subs in requested language
          2. Manual/uploaded subs in requested language
          3. Auto-generated subs in any available language
          4. Manual/uploaded subs in any available language
        """
        output_dir.mkdir(parents=True, exist_ok=True)

        strategies = [
            {
                "label": f"auto-subs ({lang})",
                "flags": ["--write-auto-sub", "--sub-lang", lang],
            },
            {
                "label": f"manual subs ({lang})",
                "flags": ["--write-sub", "--sub-lang", lang],
            },
            {
                "label": "auto-subs (any language)",
                "flags": ["--write-auto-sub", "--sub-lang", "all"],
            },
            {
                "label": "manual subs (any language)",
                "flags": ["--write-sub", "--sub-lang", "all"],
            },
        ]

        for strategy in strategies:
            label = strategy["label"]
            flags = strategy["flags"]

            if log_fn:
                log_fn(f"Trying {label}...")

            # Try JSON3 format
            result = await self._try_subtitle_download(
                url=url,
                output_dir=output_dir,
                extra_flags=flags + ["--sub-format", "json3"],
                log_fn=log_fn,
            )
            if result is not None:
                if log_fn:
                    log_fn(f"Found subtitles via {label}: {len(result)} segments")
                return result

            # Try SRT/VTT format
            result = await self._try_subtitle_download(
                url=url,
                output_dir=output_dir,
                extra_flags=flags + ["--sub-format", "srt/vtt/best"],
                log_fn=log_fn,
            )
            if result is not None:
                if log_fn:
                    log_fn(f"Found subtitles via {label}: {len(result)} segments")
                return result

        if log_fn:
            log_fn("No subtitles available from YouTube (tried all strategies)")
        return None

    async def _try_subtitle_download(
        self,
        url: str,
        output_dir: Path,
        extra_flags: list[str],
        log_fn: Callable[[str], None] | None = None,
    ) -> list[dict] | None:
        """Run a single yt-dlp subtitle download attempt and parse the result."""
        # Clean previous subtitle files to avoid stale matches
        for old in output_dir.glob("subs*"):
            old.unlink(missing_ok=True)

        sub_file = str(output_dir / "subs")
        cmd = [
            *self._base_ytdlp_cmd(),
            *extra_flags,
            "--skip-download",
            "-o", sub_file,
            url,
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        # Log yt-dlp output for debugging
        if stderr and log_fn:
            stderr_text = stderr.decode("utf-8", errors="replace").strip()
            if stderr_text:
                for line in stderr_text.splitlines()[-5:]:  # last 5 lines
                    log_fn(f"  yt-dlp: {line}")
        if proc.returncode != 0 and log_fn:
            log_fn(f"  yt-dlp exited with code {proc.returncode}")

        # Check for JSON3
        for p in output_dir.glob("subs*.json3"):
            return self._parse_json3_subs(p)

        # Check for SRT
        for p in output_dir.glob("subs*.srt"):
            return self._parse_srt_subs(p)

        # Check for VTT
        for p in output_dir.glob("subs*.vtt"):
            return self._parse_vtt_subs(p)

        return None

    # ── 2. AI Clip Detection (Gemini with key rotation) ──────────────────────

    async def find_clips_with_gemini(
        self,
        transcript: list[dict],
        instructions: str,
        api_keys: list[str],
        log_fn: Callable[[str], None] | None = None,
    ) -> list[dict]:
        """
        Use Gemini AI to find clips based on instructions.
        Supports multiple API keys — automatically rotates on rate-limit/quota errors.
        """
        if log_fn:
            log_fn("Analyzing transcript with Gemini AI...")

        # Condense transcript if too long (e.g. multi-hour livestreams)
        # Merge nearby segments to keep prompt within reasonable size
        working_transcript = transcript
        if len(transcript) > 500:
            working_transcript = self._condense_transcript(transcript, max_segments=500)
            if log_fn:
                log_fn(f"Condensed transcript: {len(transcript)} → {len(working_transcript)} segments")

        # Compute total video duration from transcript
        video_duration = max((seg["end"] for seg in working_transcript), default=0)

        # Format transcript — use total seconds to avoid ambiguity
        # e.g. [82.0s - 102.0s] instead of [1:22 - 1:42]
        transcript_text = ""
        for seg in working_transcript:
            start_s = round(seg["start"], 1)
            end_s = round(seg["end"], 1)
            transcript_text += f"[{start_s}s - {end_s}s] {seg['text']}\n"

        # Determine clip duration range from user instructions or defaults
        min_clip, max_clip = self._parse_duration_hints(instructions, video_duration)

        # Use default instructions when none provided
        effective_instructions = instructions.strip() if instructions else (
            "Find ALL interesting, notable, funny, exciting, or important moments in "
            "this video. Include highlights, key points, memorable quotes, dramatic "
            "moments, and anything a viewer would want to clip and share."
        )

        prompt = (
            "You are a video clip finder AI. You are given a transcript of a video "
            "with timestamps (in seconds), and instructions about what clips to find.\n\n"
            "IMPORTANT: The timestamps in the transcript are in SECONDS. "
            "For example, [82.0s - 102.0s] means the segment starts at 82 seconds "
            "(1 minute 22 seconds) and ends at 102 seconds (1 minute 42 seconds) "
            "into the video.\n\n"
            f"TOTAL VIDEO DURATION: {round(video_duration, 1)} seconds "
            f"({self._fmt_time(video_duration)})\n\n"
            f"TRANSCRIPT:\n{transcript_text}\n\n"
            f"INSTRUCTIONS:\n{effective_instructions}\n\n"
            "Return ONLY a valid JSON array of clips. Each clip must have:\n"
            '- "start": start time in SECONDS as a number (e.g. 82.0, NOT "1:22")\n'
            '- "end": end time in SECONDS as a number (e.g. 262.0, NOT "4:22")\n'
            '- "title": a short UNIQUE descriptive title (string, max 50 chars)\n'
            '- "reason": why this clip matches the instructions (string, max 100 chars)\n\n'
            "STRICT RULES:\n"
            f"- Each clip MUST be between {min_clip} and {max_clip} seconds long\n"
            "- start and end are in SECONDS (total seconds from video start)\n"
            "- Ensure start < end\n"
            "- NO overlapping clips — each clip must cover a DIFFERENT time range\n"
            "- NO duplicate clips — each clip must have a UNIQUE title and content\n"
            "- Each clip should capture a DIFFERENT moment from the video\n"
            "- Include context: start a few seconds before and end a few seconds "
            "after the key moment\n"
            "- Sort by start time\n"
            "- Find as many DISTINCT matching clips as possible\n\n"
            "Example response (note: start/end are in seconds):\n"
            f'[{{"start": 82.0, "end": {82.0 + min_clip}, "title": "Epic moment", '
            '"reason": "Player makes an incredible play"}, '
            f'{{"start": 350.0, "end": {350.0 + min_clip}, "title": "Funny reaction", '
            '"reason": "Streamer has hilarious reaction to jumpscare"}]'
        )

        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.3,
                "maxOutputTokens": 65536,
                "responseMimeType": "application/json",
            },
        }

        # Try each API key in order
        last_error = None
        for key_idx, api_key in enumerate(api_keys):
            key_label = f"Key #{key_idx + 1}"
            try:
                url = (
                    "https://generativelanguage.googleapis.com/v1beta/models/"
                    f"gemini-2.5-flash:generateContent?key={api_key}"
                )

                async with httpx.AsyncClient(timeout=None) as client:
                    response = await client.post(url, json=payload)

                if response.status_code == 429 or response.status_code == 403:
                    # Rate-limited or quota exceeded — try next key
                    if log_fn:
                        log_fn(f"{key_label} rate-limited (HTTP {response.status_code}), trying next key...")
                    last_error = f"HTTP {response.status_code}"
                    continue

                if response.status_code != 200:
                    raise ClipFinderError(
                        f"Gemini API error (HTTP {response.status_code}): {response.text[:500]}"
                    )

                result = response.json()

                # Parse response
                candidates = result.get("candidates", [])
                if not candidates:
                    raise ClipFinderError("Gemini returned no candidates")

                content = candidates[0].get("content", {})
                parts = content.get("parts", [])
                text = parts[0].get("text", "") if parts else ""

                clips = self._parse_clips_json(
                    text,
                    min_duration=min_clip,
                    max_duration=max_clip,
                )

                if log_fn:
                    log_fn(f"Found {len(clips)} clips matching your instructions (using {key_label})")

                return clips

            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                err_type = "timeout" if isinstance(exc, httpx.TimeoutException) else "connection error"
                if log_fn:
                    log_fn(f"{key_label} {err_type}, trying next key...")
                last_error = f"{err_type}: {exc}"
                continue

        # All keys exhausted
        raise ClipFinderError(
            f"All {len(api_keys)} Gemini API keys failed. Last error: {last_error}"
        )

    # ── 3. Download Clip Sections with yt-dlp ────────────────────────────────

    async def download_clip_sections(
        self,
        url: str,
        clips: list[dict],
        output_dir: Path,
        log_fn: Callable[[str], None] | None = None,
        index_offset: int = 0,
    ) -> list[Path]:
        """
        Download only the relevant video sections using yt-dlp --download-sections.
        Each clip is downloaded separately — no need to download the full video.

        index_offset: added to loop index for file naming (useful for single-clip download).
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        clip_paths = []

        for i, clip in enumerate(clips):
            i += index_offset
            start = clip["start"]
            end = clip["end"]

            # Sanitize title for filename
            safe_title = re.sub(r"[^\w\s-]", "", clip.get("title", f"clip_{i}"))[:40]
            safe_title = safe_title.strip().replace(" ", "_") or f"clip_{i}"
            output_file = output_dir / f"clip_{i + 1:03d}_{safe_title}.mp4"

            # Format time for yt-dlp: *SS-SS or *HH:MM:SS-HH:MM:SS
            start_fmt = self._fmt_time_hhmmss(start)
            end_fmt = self._fmt_time_hhmmss(end)

            if log_fn:
                log_fn(
                    f"Downloading clip {i + 1}/{len(clips)}: "
                    f"{self._fmt_time(start)} - {self._fmt_time(end)} "
                    f'"{clip.get("title", "")}"'
                )

            cmd = [
                *self._base_ytdlp_cmd(),
                "--concurrent-fragments", "4",
                "--download-sections", f"*{start_fmt}-{end_fmt}",
                "-f", "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
                "--merge-output-format", "mp4",
                "--no-playlist",
                "-o", str(output_file),
                "--force-keyframes-at-cuts",
                "--ppa", "ffmpeg:-c:v h264_nvenc -preset p4 -cq 23",
                url,
            ]

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()

            if proc.returncode != 0:
                err = stderr.decode(errors="replace")
                if log_fn:
                    log_fn(f"Warning: Failed to download clip {i + 1}: {err[:200]}")
                continue

            # yt-dlp might add suffix — find the actual file
            if output_file.exists():
                clip_paths.append(output_file)
            else:
                # Search for the file with potential suffix additions
                pattern = f"clip_{i + 1:03d}_{safe_title}*"
                found = list(output_dir.glob(pattern))
                if found:
                    clip_paths.append(found[0])
                else:
                    if log_fn:
                        log_fn(f"Warning: Clip {i + 1} file not found after download")
                    continue

            duration = end - start
            if log_fn:
                log_fn(f"Clip {i + 1} saved ({self._fmt_duration(duration)})")

        return clip_paths

    # ── Subtitle Parsers ─────────────────────────────────────────────────────

    def _parse_json3_subs(self, path: Path) -> list[dict]:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        segments = []
        events = data.get("events", [])
        for ev in events:
            start_ms = ev.get("tStartMs", 0)
            dur_ms = ev.get("dDurationMs", 0)
            segs = ev.get("segs", [])
            text = "".join(s.get("utf8", "") for s in segs).strip()
            text = re.sub(r"\n", " ", text)
            if text and text != "\n":
                segments.append({
                    "start": start_ms / 1000.0,
                    "end": (start_ms + dur_ms) / 1000.0,
                    "text": text,
                })

        return self._merge_short_segments(segments)

    def _parse_srt_subs(self, path: Path) -> list[dict]:
        content = path.read_text(encoding="utf-8", errors="replace")
        return self._parse_timed_text(content)

    def _parse_vtt_subs(self, path: Path) -> list[dict]:
        content = path.read_text(encoding="utf-8", errors="replace")
        return self._parse_timed_text(content)

    def _parse_timed_text(self, content: str) -> list[dict]:
        segments = []
        blocks = re.split(r"\n\n+", content.strip())

        for block in blocks:
            lines = block.strip().split("\n")
            ts_line = None
            text_lines = []
            for line in lines:
                if "-->" in line:
                    ts_line = line
                elif ts_line is not None:
                    text_lines.append(line)

            if not ts_line or not text_lines:
                continue

            ts_match = re.search(
                r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{3})",
                ts_line,
            )
            if not ts_match:
                ts_match = re.search(
                    r"(\d{1,2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{1,2}):(\d{2})[,.](\d{3})",
                    ts_line,
                )
                if ts_match:
                    g = ts_match.groups()
                    start = int(g[0]) * 60 + int(g[1]) + int(g[2]) / 1000
                    end = int(g[3]) * 60 + int(g[4]) + int(g[5]) / 1000
                else:
                    continue
            else:
                g = ts_match.groups()
                start = int(g[0]) * 3600 + int(g[1]) * 60 + int(g[2]) + int(g[3]) / 1000
                end = int(g[4]) * 3600 + int(g[5]) * 60 + int(g[6]) + int(g[7]) / 1000

            text = " ".join(text_lines).strip()
            text = re.sub(r"<[^>]+>", "", text)
            if text:
                segments.append({"start": start, "end": end, "text": text})

        return self._merge_short_segments(segments)

    @staticmethod
    def _merge_short_segments(
        segments: list[dict], gap: float = 1.0, max_len: int = 200
    ) -> list[dict]:
        if not segments:
            return segments

        merged = [segments[0].copy()]
        for seg in segments[1:]:
            prev = merged[-1]
            if (seg["start"] - prev["end"]) < gap and len(prev["text"]) < max_len:
                prev["end"] = seg["end"]
                prev["text"] = prev["text"] + " " + seg["text"]
            else:
                merged.append(seg.copy())

        return merged

    @staticmethod
    def _condense_transcript(
        segments: list[dict], max_segments: int = 500
    ) -> list[dict]:
        """Merge segments to fit within max_segments for large transcripts.

        Progressively increases the merge gap until the segment count
        drops below max_segments. Preserves timestamps so Gemini can
        still identify clip boundaries accurately.
        """
        if len(segments) <= max_segments:
            return segments

        # Progressively merge with increasing gap thresholds
        for gap in [2.0, 4.0, 8.0, 15.0, 30.0]:
            merged = [segments[0].copy()]
            for seg in segments[1:]:
                prev = merged[-1]
                if (seg["start"] - prev["end"]) < gap:
                    prev["end"] = seg["end"]
                    prev["text"] = prev["text"] + " " + seg["text"]
                else:
                    merged.append(seg.copy())

            if len(merged) <= max_segments:
                return merged

        return merged

    # ── JSON Parsing ─────────────────────────────────────────────────────────

    def _parse_clips_json(
        self, text: str, min_duration: float = 10.0, max_duration: float = 300.0
    ) -> list[dict]:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            json_match = re.search(r"\[.*\]", text, re.DOTALL)
            if json_match:
                try:
                    data = json.loads(json_match.group())
                except json.JSONDecodeError:
                    # Truncated JSON — try to salvage complete objects
                    data = self._salvage_truncated_json(text)
            else:
                # No closing bracket — likely truncated output
                data = self._salvage_truncated_json(text)

        if not isinstance(data, list):
            raise ClipFinderError("Gemini response is not a JSON array")

        valid_clips = []
        for clip in data:
            if not isinstance(clip, dict):
                continue
            if all(k in clip for k in ("start", "end", "title")):
                try:
                    clip["start"] = self._to_seconds(clip["start"])
                    clip["end"] = self._to_seconds(clip["end"])
                    clip["title"] = str(clip.get("title", "Clip"))[:60]
                    clip["reason"] = str(clip.get("reason", ""))[:150]
                    duration = clip["end"] - clip["start"]
                    if duration < 1.0:
                        # Skip clips shorter than 1 second (clearly invalid)
                        continue
                    if clip["end"] > clip["start"]:
                        valid_clips.append(clip)
                except (ValueError, TypeError):
                    continue

        # Enforce minimum duration — extend short clips to at least min_duration
        for clip in valid_clips:
            duration = clip["end"] - clip["start"]
            if duration < min_duration:
                # Extend the clip symmetrically around its center
                center = (clip["start"] + clip["end"]) / 2
                half = min_duration / 2
                clip["start"] = max(0, center - half)
                clip["end"] = center + half

        # Cap clips that exceed max_duration
        for clip in valid_clips:
            duration = clip["end"] - clip["start"]
            if duration > max_duration:
                clip["end"] = clip["start"] + max_duration

        # Deduplicate — remove clips with overlapping time ranges or identical titles
        valid_clips = self._deduplicate_clips(valid_clips)

        return valid_clips

    @staticmethod
    def _deduplicate_clips(clips: list[dict]) -> list[dict]:
        """Remove duplicate/overlapping clips. Keep the first occurrence."""
        if not clips:
            return clips

        # Sort by start time
        clips.sort(key=lambda c: c["start"])

        deduped = []
        seen_titles: set[str] = set()

        for clip in clips:
            title_key = clip["title"].strip().lower()

            # Skip exact duplicate titles
            if title_key in seen_titles:
                continue

            # Skip clips that overlap significantly with an already accepted clip
            overlap = False
            for accepted in deduped:
                # Calculate overlap ratio
                overlap_start = max(clip["start"], accepted["start"])
                overlap_end = min(clip["end"], accepted["end"])
                if overlap_end > overlap_start:
                    overlap_duration = overlap_end - overlap_start
                    clip_duration = clip["end"] - clip["start"]
                    if clip_duration > 0 and (overlap_duration / clip_duration) > 0.5:
                        overlap = True
                        break

            if not overlap:
                deduped.append(clip)
                seen_titles.add(title_key)

        return deduped

    @staticmethod
    def _salvage_truncated_json(text: str) -> list[dict]:
        """Extract complete JSON objects from a truncated JSON array.

        When Gemini's output is cut off mid-array (e.g. maxOutputTokens hit),
        we find all complete {...} objects and parse them individually.
        """
        objects = []
        # Find all complete JSON objects in the text
        depth = 0
        start_idx = None
        for i, ch in enumerate(text):
            if ch == '{':
                if depth == 0:
                    start_idx = i
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0 and start_idx is not None:
                    obj_str = text[start_idx:i + 1]
                    try:
                        obj = json.loads(obj_str)
                        if isinstance(obj, dict):
                            objects.append(obj)
                    except json.JSONDecodeError:
                        pass
                    start_idx = None

        if not objects:
            raise ClipFinderError(f"Failed to parse Gemini response: {text[:300]}")

        logger.warning(
            "Salvaged {} complete clip(s) from truncated Gemini response",
            len(objects),
        )
        return objects

    # ── Helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_duration_hints(
        instructions: str, video_duration: float
    ) -> tuple[float, float]:
        """Parse min/max clip duration from user instructions.

        Looks for patterns like "2-3 menit", "1-2 minutes", "30 detik", "60s clips"
        and returns (min_seconds, max_seconds).  Falls back to sensible defaults
        based on overall video length.
        """
        min_clip = 15.0   # default minimum
        max_clip = 120.0  # default maximum

        if not instructions:
            return min_clip, max_clip

        text = instructions.lower()

        # Pattern: "X-Y menit/minutes/min" — range in minutes
        m = re.search(
            r'(\d+(?:[.,]\d+)?)\s*[-–~]\s*(\d+(?:[.,]\d+)?)\s*'
            r'(?:menit|minutes?|mins?)',
            text,
        )
        if m:
            lo = float(m.group(1).replace(",", "."))
            hi = float(m.group(2).replace(",", "."))
            min_clip = lo * 60
            max_clip = hi * 60
            return min_clip, max_clip

        # Pattern: "X-Y detik/seconds/sec/s" — range in seconds
        m = re.search(
            r'(\d+(?:[.,]\d+)?)\s*[-–~]\s*(\d+(?:[.,]\d+)?)\s*'
            r'(?:detik|seconds?|secs?|s\b)',
            text,
        )
        if m:
            lo = float(m.group(1).replace(",", "."))
            hi = float(m.group(2).replace(",", "."))
            min_clip = lo
            max_clip = hi
            return min_clip, max_clip

        # Pattern: "X menit/minutes" — single value treated as target
        m = re.search(
            r'(\d+(?:[.,]\d+)?)\s*(?:menit|minutes?|mins?)', text
        )
        if m:
            target = float(m.group(1).replace(",", ".")) * 60
            min_clip = max(10, target * 0.5)
            max_clip = target * 1.5
            return min_clip, max_clip

        # Pattern: "X detik/seconds" — single value treated as target
        m = re.search(
            r'(\d+(?:[.,]\d+)?)\s*(?:detik|seconds?|secs?)', text
        )
        if m:
            target = float(m.group(1).replace(",", "."))
            min_clip = max(5, target * 0.5)
            max_clip = target * 1.5
            return min_clip, max_clip

        # Adaptive defaults based on video length
        if video_duration > 3600:         # > 1 hour
            min_clip = 30
            max_clip = 300
        elif video_duration > 600:        # > 10 minutes
            min_clip = 15
            max_clip = 180
        else:                             # short videos
            min_clip = 10
            max_clip = 120

        return min_clip, max_clip

    @staticmethod
    def filter_transcript_by_offset(
        transcript: list[dict], start_offset: float
    ) -> list[dict]:
        """Filter transcript segments to only include those after start_offset.

        Used for livestream videos where the first N minutes are waiting time.
        """
        if start_offset <= 0:
            return transcript
        return [seg for seg in transcript if seg["end"] > start_offset]

    @staticmethod
    def slice_transcript_for_clip(
        transcript: list[dict],
        clip_start: float,
        clip_end: float,
        padding: float = 0.5,
    ) -> list[dict]:
        """Slice and re-time transcript segments for a specific clip.

        Extracts only the segments that overlap with the clip's time range,
        then adjusts timestamps to be relative to clip start (0-based),
        since the downloaded clip MP4 starts at 0:00.

        Parameters
        ----------
        transcript : list[dict]
            Full YouTube auto-sub transcript, each dict has {start, end, text}.
        clip_start : float
            Clip start time in the original video (seconds).
        clip_end : float
            Clip end time in the original video (seconds).
        padding : float
            Extra seconds before/after clip boundaries to include
            partial-overlap segments.

        Returns
        -------
        list[dict]
            Sliced transcript with timestamps relative to clip start (0-based).
            Each dict: {start: float, end: float, text: str, source: "autosub"}
        """
        clip_duration = clip_end - clip_start
        sliced: list[dict] = []

        for seg in transcript:
            seg_start = seg["start"]
            seg_end = seg["end"]

            # Skip segments entirely outside the clip range (with padding)
            if seg_end < clip_start - padding:
                continue
            if seg_start > clip_end + padding:
                continue

            # Re-time to clip-relative (0-based)
            new_start = max(0.0, seg_start - clip_start)
            new_end = min(clip_duration, seg_end - clip_start)

            # Discard very short segments after clipping
            if new_end - new_start < 0.1:
                continue

            sliced.append({
                "start": round(new_start, 3),
                "end": round(new_end, 3),
                "text": seg["text"],
                "source": "autosub",
            })

        return sliced

    @staticmethod
    def _fmt_time(secs: float) -> str:
        h = int(secs // 3600)
        m = int((secs % 3600) // 60)
        s = int(secs % 60)
        if h > 0:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m}:{s:02d}"

    @staticmethod
    def _to_seconds(value) -> float:
        """Convert a value to total seconds.

        Handles:
          - int/float: returned as-is
          - "123.5": plain number string
          - "1:22": M:SS → 82 seconds
          - "1:02:30": H:MM:SS → 3750 seconds
        """
        if isinstance(value, (int, float)):
            return float(value)

        s = str(value).strip()
        # Try plain float first
        try:
            return float(s)
        except ValueError:
            pass

        # Try H:MM:SS or M:SS
        parts = s.split(":")
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])

        raise ValueError(f"Cannot convert {value!r} to seconds")

    @staticmethod
    def _fmt_time_hhmmss(secs: float) -> str:
        """Format as HH:MM:SS.mm for yt-dlp --download-sections."""
        h = int(secs // 3600)
        m = int((secs % 3600) // 60)
        s = secs % 60
        return f"{h:02d}:{m:02d}:{s:05.2f}"

    @staticmethod
    def _fmt_duration(secs: float) -> str:
        m = int(secs // 60)
        s = int(secs % 60)
        if m > 0:
            return f"{m}m {s}s"
        return f"{s}s"
