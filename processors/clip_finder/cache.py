"""
processors/clip_finder/cache.py — Disk-backed caching for transcript and signals.

Avoids re-downloading subtitles / re-running audio analysis when the user
tweaks instructions and re-runs the find-clips action against the same URL.

Cache key = sha1(url) — instructions are NOT part of the key because they
only affect the LLM step downstream of cache.

File layout (under <output_dir>/cache/):
  <hash>/
    transcript.json
    signals.json
    meta.json        # { url, fetched_at, segment_count, signal_count }
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Sequence

from models.clip import SignalEvent


class TranscriptCache:
    """Cache transcript + multimodal signals keyed by URL."""

    def __init__(self, root: Path):
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)

    def _bucket(self, url: str) -> Path:
        h = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
        return self._root / h

    # ── transcript ──────────────────────────────────────────────────────

    def load_transcript(self, url: str, max_age_hours: float = 168) -> list[dict] | None:
        bucket = self._bucket(url)
        meta_p = bucket / "meta.json"
        ts_p = bucket / "transcript.json"
        if not (meta_p.exists() and ts_p.exists()):
            return None
        try:
            meta = json.loads(meta_p.read_text("utf-8"))
            age_hours = (time.time() - meta.get("fetched_at", 0)) / 3600.0
            if age_hours > max_age_hours:
                return None
            return json.loads(ts_p.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def save_transcript(self, url: str, transcript: Sequence[dict]) -> None:
        bucket = self._bucket(url)
        bucket.mkdir(parents=True, exist_ok=True)
        meta_p = bucket / "meta.json"
        ts_p = bucket / "transcript.json"

        meta = {}
        if meta_p.exists():
            try:
                meta = json.loads(meta_p.read_text("utf-8"))
            except json.JSONDecodeError:
                pass
        meta.update({
            "url": url,
            "fetched_at": time.time(),
            "segment_count": len(transcript),
        })
        ts_p.write_text(
            json.dumps(list(transcript), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        meta_p.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    # ── signals ─────────────────────────────────────────────────────────

    def load_signals(self, url: str, max_age_hours: float = 168) -> list[SignalEvent] | None:
        bucket = self._bucket(url)
        sig_p = bucket / "signals.json"
        meta_p = bucket / "meta.json"
        if not (sig_p.exists() and meta_p.exists()):
            return None
        try:
            meta = json.loads(meta_p.read_text("utf-8"))
            age_hours = (time.time() - meta.get("fetched_at", 0)) / 3600.0
            if age_hours > max_age_hours:
                return None
            raw = json.loads(sig_p.read_text("utf-8"))
            return [SignalEvent.from_dict(s) for s in raw if isinstance(s, dict)]
        except (json.JSONDecodeError, OSError):
            return None

    def save_signals(self, url: str, signals: Sequence[SignalEvent]) -> None:
        bucket = self._bucket(url)
        bucket.mkdir(parents=True, exist_ok=True)
        meta_p = bucket / "meta.json"
        sig_p = bucket / "signals.json"

        meta = {}
        if meta_p.exists():
            try:
                meta = json.loads(meta_p.read_text("utf-8"))
            except json.JSONDecodeError:
                pass
        meta.update({
            "url": url,
            "fetched_at": time.time(),
            "signal_count": len(signals),
        })
        sig_p.write_text(
            json.dumps([s.to_dict() for s in signals], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        meta_p.write_text(json.dumps(meta, indent=2), encoding="utf-8")


__all__ = ["TranscriptCache"]
