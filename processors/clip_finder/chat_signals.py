"""
processors/clip_finder/chat_signals.py — YouTube live-chat replay mining.

Extracts chat-spike, emote-storm, and superchat events from a YouTube
video's chat replay (`live_chat` json subtitle track that yt-dlp can
already download).

For VTuber clip discovery this is gold: chat-spike + audio-peak overlap
is the single highest-precision predictor of a clip-worthy moment.
"""

from __future__ import annotations

import asyncio
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any, Callable

import yt_dlp
import yt_dlp.utils

from models.clip import SignalEvent, SignalKind

LogFn = Callable[[str], None]


# ─── Tunables ────────────────────────────────────────────────────────────────

WINDOW_SECONDS = 5         # bucket size for chat-velocity
SPIKE_RATIO = 3.0          # msgs/sec ≥ baseline × N → CHAT_SPIKE
EMOTE_STORM_MIN = 5         # same emote ≥ K times in WINDOW_SECONDS
SUPERCHAT_KEYS = (          # YT live-chat renderer types that carry money
    "liveChatPaidMessageRenderer",
    "liveChatPaidStickerRenderer",
    "liveChatSponsorshipsGiftPurchaseAnnouncementRenderer",
)


class ChatSignalExtractor:
    """Downloads chat replay JSON, returns chat-derived SignalEvents."""

    def __init__(self, cookies_file: str = "", cookies_browser: str = ""):
        self._cookies_file = cookies_file
        self._cookies_browser = cookies_browser

    async def extract(
        self,
        url: str,
        output_dir: Path,
        log_fn: LogFn | None = None,
    ) -> list[SignalEvent]:
        output_dir.mkdir(parents=True, exist_ok=True)
        chat_path = await self._download_live_chat(url, output_dir, log_fn)
        if not chat_path:
            return []

        try:
            messages = self._parse_live_chat(chat_path)
        except Exception as exc:
            if log_fn:
                log_fn(f"ChatSignals: parse failed: {exc}")
            return []

        if not messages:
            if log_fn:
                log_fn("ChatSignals: no chat messages found in replay")
            return []

        events: list[SignalEvent] = []
        events.extend(self._compute_velocity_spikes(messages))
        events.extend(self._compute_emote_storms(messages))
        events.extend(self._collect_superchats(messages))

        events.sort(key=lambda e: e.start)
        if log_fn:
            counts = Counter(e.kind.value for e in events)
            log_fn(
                f"ChatSignals: {counts.get('chat_spike', 0)} spikes, "
                f"{counts.get('chat_emote', 0)} emote storms, "
                f"{counts.get('chat_superchat', 0)} superchats "
                f"(from {len(messages)} messages)"
            )
        return events

    # ── live_chat download ───────────────────────────────────────────────

    async def _download_live_chat(
        self, url: str, output_dir: Path, log_fn: LogFn | None
    ) -> Path | None:
        # Wipe any stale chat artifact
        for old in output_dir.glob("livechat*"):
            old.unlink(missing_ok=True)

        out_template = str(output_dir / "livechat")
        ydl_opts: dict = {
            "skip_download": True,
            "writesubtitles": True,
            "subtitleslangs": ["live_chat"],
            "outtmpl": out_template,
            "quiet": True,
            "noplaylist": True,
            "ignore_no_formats_error": True,
            "extractor_args": {
                "youtube": {"player_client": ["default", "android_vr"]},
            },
        }
        if self._cookies_file:
            ydl_opts["cookiefile"] = self._cookies_file
            ydl_opts["extractor_args"] = {
                "youtube": {"player_client": ["tv_downgraded", "tv", "web_creator"]}
            }
        elif self._cookies_browser:
            ydl_opts["cookiesfrombrowser"] = (self._cookies_browser,)
            ydl_opts["extractor_args"] = {
                "youtube": {"player_client": ["tv_downgraded", "tv", "web_creator"]}
            }

        if log_fn:
            log_fn("ChatSignals: downloading live_chat replay (no video)...")

        def _run() -> None:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                try:
                    ydl.download([url])
                except yt_dlp.utils.DownloadError:
                    pass

        await asyncio.to_thread(_run)

        for ext in ("json", "live_chat.json"):
            for p in output_dir.glob(f"livechat*.{ext}"):
                return p
        # yt-dlp historically used ".live_chat.json" extension
        for p in output_dir.glob("livechat*"):
            if p.suffix.lower() in {".json", ".live_chat.json"}:
                return p
        if log_fn:
            log_fn("ChatSignals: no live_chat track available for this video")
        return None

    # ── live_chat parser ────────────────────────────────────────────────

    @staticmethod
    def _parse_live_chat(path: Path) -> list[dict[str, Any]]:
        """yt-dlp emits one JSON event per line for live chat replay."""
        msgs: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                offset_ms = ChatSignalExtractor._extract_offset_ms(obj)
                if offset_ms is None:
                    continue
                renderer_type, text, emotes, is_super = (
                    ChatSignalExtractor._extract_message_payload(obj)
                )
                msgs.append({
                    "t": offset_ms / 1000.0,
                    "type": renderer_type,
                    "text": text,
                    "emotes": emotes,
                    "is_super": is_super,
                })
        return msgs

    @staticmethod
    def _extract_offset_ms(event: dict) -> int | None:
        """Walk the live-chat replay action tree to find videoOffsetTimeMsec."""
        actions = (
            event.get("replayChatItemAction", {}).get("actions", [])
            or [event]
        )
        offset_raw = (
            event.get("replayChatItemAction", {})
            .get("videoOffsetTimeMsec")
        )
        if offset_raw is not None:
            try:
                return int(offset_raw)
            except (TypeError, ValueError):
                pass
        # Fallback: dig into addChatItemAction → not always offset-tagged
        for act in actions:
            for key in ("addChatItemAction", "addLiveChatTickerItemAction"):
                if key in act:
                    return None  # offset only available on outer wrapper
        return None

    @staticmethod
    def _extract_message_payload(event: dict) -> tuple[str, str, list[str], bool]:
        """Find the message renderer and pull text + emotes + super flag."""
        actions = (
            event.get("replayChatItemAction", {}).get("actions", [])
            or [event]
        )
        for act in actions:
            add = act.get("addChatItemAction", {}).get("item", {})
            for renderer_key, renderer_obj in add.items():
                if not isinstance(renderer_obj, dict):
                    continue
                text, emotes = ChatSignalExtractor._render_runs(
                    renderer_obj.get("message", {}).get("runs", [])
                )
                is_super = renderer_key in SUPERCHAT_KEYS
                return renderer_key, text, emotes, is_super
        return "unknown", "", [], False

    @staticmethod
    def _render_runs(runs: list) -> tuple[str, list[str]]:
        text_parts: list[str] = []
        emotes: list[str] = []
        for run in runs:
            if "text" in run:
                text_parts.append(run["text"])
            if "emoji" in run:
                emoji = run["emoji"]
                # Distinguish unicode emoji vs custom channel emote
                if emoji.get("isCustomEmoji"):
                    shortcuts = emoji.get("shortcuts") or []
                    label = (shortcuts[0] if shortcuts else "custom_emote")
                    emotes.append(label)
                else:
                    label = emoji.get("emojiId", "")
                    if label:
                        emotes.append(label)
        return " ".join(text_parts).strip(), emotes

    # ── analytics ────────────────────────────────────────────────────────

    @staticmethod
    def _compute_velocity_spikes(messages: list[dict]) -> list[SignalEvent]:
        if not messages:
            return []
        max_t = max(m["t"] for m in messages)
        n_buckets = int(math.ceil((max_t + 1) / WINDOW_SECONDS))
        if n_buckets < 4:
            return []

        counts = [0] * n_buckets
        for m in messages:
            idx = int(m["t"] // WINDOW_SECONDS)
            if 0 <= idx < n_buckets:
                counts[idx] += 1

        # Use median of all buckets as baseline
        sorted_counts = sorted(counts)
        baseline = sorted_counts[len(sorted_counts) // 2]
        if baseline < 1:
            baseline = max(1.0, sum(counts) / max(1, len(counts)) * 0.5)

        events: list[SignalEvent] = []
        i = 0
        while i < n_buckets:
            ratio = counts[i] / baseline if baseline else 0
            if ratio >= SPIKE_RATIO and counts[i] >= 5:
                # Cluster contiguous high-velocity buckets
                j = i
                while j + 1 < n_buckets and (counts[j + 1] / baseline) >= SPIKE_RATIO * 0.7:
                    j += 1
                start = i * WINDOW_SECONDS
                end = (j + 1) * WINDOW_SECONDS
                peak_ratio = max(counts[k] / baseline for k in range(i, j + 1))
                events.append(SignalEvent(
                    kind=SignalKind.CHAT_SPIKE,
                    start=float(start),
                    end=float(end),
                    intensity=min(1.0, peak_ratio / 10.0),
                    label=f"chat {peak_ratio:.1f}x baseline",
                ))
                i = j + 1
            else:
                i += 1
        return events

    @staticmethod
    def _compute_emote_storms(messages: list[dict]) -> list[SignalEvent]:
        if not messages:
            return []

        events: list[SignalEvent] = []
        # Sliding window count of each emote across WINDOW_SECONDS
        max_t = max(m["t"] for m in messages)
        n_buckets = int(math.ceil((max_t + 1) / WINDOW_SECONDS))
        bucket_emotes: list[Counter] = [Counter() for _ in range(n_buckets)]
        for m in messages:
            idx = int(m["t"] // WINDOW_SECONDS)
            if 0 <= idx < n_buckets:
                for e in m.get("emotes", []):
                    bucket_emotes[idx][e] += 1

        for idx, counter in enumerate(bucket_emotes):
            if not counter:
                continue
            label, count = counter.most_common(1)[0]
            if count >= EMOTE_STORM_MIN:
                start = idx * WINDOW_SECONDS
                end = (idx + 1) * WINDOW_SECONDS
                events.append(SignalEvent(
                    kind=SignalKind.CHAT_EMOTE_STORM,
                    start=float(start),
                    end=float(end),
                    intensity=min(1.0, count / 30.0),
                    label=f"{count}x {label}",
                    sample=label,
                ))
        return events

    @staticmethod
    def _collect_superchats(messages: list[dict]) -> list[SignalEvent]:
        events: list[SignalEvent] = []
        for m in messages:
            if not m.get("is_super"):
                continue
            t = float(m["t"])
            events.append(SignalEvent(
                kind=SignalKind.CHAT_SUPERCHAT,
                start=t,
                end=t + 5.0,
                intensity=0.8,
                label="superchat / sticker / sponsorship",
                sample=(m.get("text") or "")[:80],
            ))
        return events


__all__ = ["ChatSignalExtractor"]
