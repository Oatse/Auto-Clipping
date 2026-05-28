#!/usr/bin/env python3
"""
audit_segmented_json.py — A/B test: ElevenLabs Scribe v2 ``segmented_json``
vs our :meth:`processors.stt.elevenlabs.ElevenLabsSttEngine._parse_response`
heuristic on the same audio file.

Run from repo root (so ``import config`` resolves)::

    python audit_segmented_json.py path\\to\\video.mp4
    python audit_segmented_json.py path\\to\\video.mp4 --out tmp\\audit
    python audit_segmented_json.py path\\to\\video.mp4 --model scribe_v2 --no-verbatim
    python audit_segmented_json.py path\\to\\video.mp4 --keyterms VTuber Hololive

Produces in ``<out>``::

    raw_response.json   full ElevenLabs response (words[] + additional_formats[])
    el_segments.json    server-side segmented_json (decoded)
    our_segments.json   our _parse_response output
    summary.txt         side-by-side stats

This is a one-off pilot for the audit report's P1.2 decision — NOT part
of the pytest suite. The file is ignored by pytest because the name
doesn't match ``test_*.py``.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import statistics
import sys
from pathlib import Path
from typing import Any

import httpx

# Repo deps. Run from repo root for these to resolve.
import config
from processors.stt.elevenlabs import (
    API_URL,
    SENTENCE_TERMINATORS,
    ElevenLabsSttEngine,
)
from utils.ffmpeg_utils import extract_audio
from utils.file_utils import ensure_dir


# Defaults below mirror the documented SRT defaults from the OpenAPI
# spec. For ``segmented_json`` the spec lists no defaults, so we feed
# the same numbers explicitly to keep the output comparable across runs.
SEGMENTED_FORMAT: dict[str, Any] = {
    "format": "segmented_json",
    "include_speakers": True,
    "include_timestamps": True,
    "segment_on_silence_longer_than_s": 0.8,
    "max_segment_duration_s": 4,
    "max_segment_chars": 84,
}


async def call_api(
    audio_path: Path,
    *,
    model: str,
    diarize: bool,
    language_code: str | None,
    no_verbatim: bool,
    keyterms: list[str] | None,
) -> dict:
    """POST audio + flags to Scribe, requesting word-level + segmented_json."""
    if not config.ELEVENLABS_API_KEYS:
        raise RuntimeError("No ELEVENLABS_API_KEYS configured in .env")
    api_key = config.ELEVENLABS_API_KEYS[0]

    data: dict[str, Any] = {
        "model_id": model,
        "timestamps_granularity": "word",
        "diarize": str(diarize).lower(),
        "tag_audio_events": "false",
        "additional_formats": json.dumps([SEGMENTED_FORMAT]),
    }
    if language_code:
        data["language_code"] = language_code
    if no_verbatim:
        data["no_verbatim"] = "true"
    if keyterms:
        data["keyterms"] = json.dumps(keyterms)

    async with httpx.AsyncClient(timeout=600.0) as client:
        with audio_path.open("rb") as f:
            files = {"file": (audio_path.name, f, "audio/wav")}
            resp = await client.post(
                API_URL,
                headers={"xi-api-key": api_key},
                files=files,
                data=data,
            )

    if resp.status_code != 200:
        raise RuntimeError(
            f"ElevenLabs API HTTP {resp.status_code}: {resp.text[:300]}"
        )
    return resp.json()


def extract_segmented_json(raw: dict) -> list[dict] | None:
    """Pull and decode the ``segmented_json`` payload from ``additional_formats``.

    The server returns a list of ``AdditionalFormatResponseModel`` entries.
    Each entry's ``content`` is a string that may be base64-encoded; the
    schema in the OpenAPI spec calls this out via ``is_base64_encoded``.
    """
    formats = raw.get("additional_formats") or []
    for fmt in formats:
        if fmt.get("requested_format") != "segmented_json":
            continue
        content = fmt.get("content", "")
        if fmt.get("is_base64_encoded"):
            content = base64.b64decode(content).decode("utf-8")
        data = json.loads(content)
        # Defensive: docs don't pin the top-level shape. Accept either a
        # bare list of segments or a dict wrapper with a ``segments`` key.
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("segments"), list):
            return data["segments"]
        # Unknown shape — surface raw for inspection.
        print(
            f"WARNING: unexpected segmented_json shape: {type(data).__name__}",
            file=sys.stderr,
        )
        return None
    return None


def _word_field(word: dict, *names: str, default: Any = None) -> Any:
    """Pluck the first present field from a word dict.

    ElevenLabs ``segmented_json`` words use ``text`` / ``speaker_id``;
    our ``TranscriptSegment`` words use ``word`` / (no per-word speaker).
    """
    for name in names:
        if name in word and word[name] is not None:
            return word[name]
    return default


def _segment_bounds(seg: dict) -> tuple[float, float]:
    """Return (start, end) for a segment.

    Prefers segment-level fields when present (our shape) and falls back
    to first/last word timestamps (segmented_json shape).
    """
    if seg.get("start") is not None and seg.get("end") is not None:
        return float(seg["start"]), float(seg["end"])
    words = seg.get("words") or []
    if not words:
        return 0.0, 0.0
    return float(words[0].get("start", 0.0)), float(words[-1].get("end", 0.0))


def _segment_speaker(seg: dict) -> str:
    """Best-effort speaker label.

    Our shape stores it at segment level; ``segmented_json`` only carries
    it per-word, so fall back to the majority speaker_id of the words.
    """
    if seg.get("speaker"):
        return str(seg["speaker"])
    speakers = [
        _word_field(w, "speaker_id", default=None)
        for w in (seg.get("words") or [])
    ]
    speakers = [s for s in speakers if s]
    if not speakers:
        return "?"
    # Majority vote — single-speaker segments are the common case.
    return max(set(speakers), key=speakers.count)


def stats_for_segments(segments: list[dict]) -> dict[str, Any]:
    """Compute summary metrics over a list of segment dicts.

    Handles two shapes:
      * Our ``TranscriptSegment.to_dict()``: top-level start/end/text/speaker.
      * ElevenLabs ``segmented_json``: only text + words[].
    """
    if not segments:
        return {"count": 0}

    durations: list[float] = []
    word_counts: list[int] = []
    char_counts: list[int] = []
    ends_with_term = 0
    multi_speaker_segs = 0
    speaker_changes = 0
    prev_speaker: str | None = None
    punct_only = 0

    for seg in segments:
        start, end = _segment_bounds(seg)
        durations.append(max(0.0, end - start))

        text = (seg.get("text") or "").strip()
        char_counts.append(len(text))
        if text and text[-1] in SENTENCE_TERMINATORS:
            ends_with_term += 1
        # Loose punct-only check across both ASCII + CJK terminators.
        if text and all(c in SENTENCE_TERMINATORS or c.isspace() for c in text):
            punct_only += 1

        words = seg.get("words") or []
        if words:
            word_counts.append(len(words))
        else:
            word_counts.append(len(text.split()) if text else 0)

        # Per-segment multi-speaker detection (segmented_json only — our
        # shape collapses each segment to one speaker by construction).
        speakers_in_seg = {
            _word_field(w, "speaker_id", default=None) for w in words
        }
        speakers_in_seg.discard(None)
        if len(speakers_in_seg) > 1:
            multi_speaker_segs += 1

        speaker = _segment_speaker(seg)
        if prev_speaker is not None and speaker != prev_speaker:
            speaker_changes += 1
        prev_speaker = speaker

    n = len(segments)
    return {
        "count": n,
        "duration_avg_s": round(statistics.mean(durations), 3),
        "duration_median_s": round(statistics.median(durations), 3),
        "duration_max_s": round(max(durations), 3),
        "duration_min_s": round(min(durations), 3),
        "words_avg": round(statistics.mean(word_counts), 2),
        "words_median": int(statistics.median(word_counts)),
        "words_max": max(word_counts),
        "chars_avg": round(statistics.mean(char_counts), 1),
        "chars_max": max(char_counts),
        "ends_with_terminator_pct": round(ends_with_term / n * 100, 1),
        "multi_speaker_segments": multi_speaker_segs,
        "speaker_changes": speaker_changes,
        "punctuation_only_segments": punct_only,
    }


def write_summary(
    out_path: Path,
    *,
    audio_path: Path,
    model: str,
    raw: dict,
    el_stats: dict | None,
    our_stats: dict,
) -> None:
    lines: list[str] = []
    lines.append(f"Audio: {audio_path.name}")
    lines.append(f"Model: {model}")
    lines.append(
        f"Language: {raw.get('language_code')!r} "
        f"(probability {raw.get('language_probability')})"
    )
    lines.append(f"Duration: {raw.get('audio_duration_secs')} s")
    lines.append(f"Total flat words: {len(raw.get('words', []))}")
    lines.append("")
    lines.append("=== Our _parse_response ===")
    for k, v in our_stats.items():
        lines.append(f"  {k}: {v}")
    lines.append("")
    lines.append("=== ElevenLabs segmented_json ===")
    if el_stats is None:
        lines.append("  (segmented_json NOT present in response)")
    else:
        for k, v in el_stats.items():
            lines.append(f"  {k}: {v}")
    lines.append("")

    if el_stats:
        lines.append("=== Delta (ours - elevenlabs) ===")
        for k in sorted(set(our_stats) & set(el_stats)):
            o, e = our_stats[k], el_stats[k]
            if isinstance(o, (int, float)) and isinstance(e, (int, float)):
                lines.append(f"  {k}: {round(o - e, 3)}")
        lines.append("")

    lines.append("Files in this directory:")
    lines.append("  raw_response.json - full ElevenLabs response")
    lines.append("  el_segments.json  - segmented_json from additional_formats")
    lines.append("  our_segments.json - our _parse_response output")
    lines.append("")
    lines.append(
        "Compare el_segments.json vs our_segments.json to decide P1.2 (audit)."
    )

    out_path.write_text("\n".join(lines), encoding="utf-8")


async def run(args: argparse.Namespace) -> int:
    video_path = Path(args.video).resolve()
    if not video_path.exists():
        print(f"Error: file not found: {video_path}", file=sys.stderr)
        return 2

    out_dir = Path(args.out).resolve()
    ensure_dir(out_dir)

    audio_path = out_dir / f"{video_path.stem}.wav"
    print(f"[1/4] Extract audio  -> {audio_path.name}")
    await extract_audio(video_path, audio_path)

    print(f"[2/4] Scribe call    -> model={args.model} diarize={args.diarize} "
          f"no_verbatim={args.no_verbatim} keyterms={bool(args.keyterms)}")
    raw = await call_api(
        audio_path,
        model=args.model,
        diarize=args.diarize,
        language_code=args.language_code,
        no_verbatim=args.no_verbatim,
        keyterms=args.keyterms,
    )
    (out_dir / "raw_response.json").write_text(
        json.dumps(raw, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print("[3/4] Parse via our _parse_response")
    engine = ElevenLabsSttEngine()
    our_segs = engine._parse_response(raw, speaker_detection=args.diarize)
    our_dict = [s.to_dict() for s in our_segs]
    (out_dir / "our_segments.json").write_text(
        json.dumps(our_dict, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print("[3/4] Decode segmented_json from additional_formats[]")
    el_segs = extract_segmented_json(raw)
    if el_segs is None:
        print("      WARNING: segmented_json missing or unparseable")
    (out_dir / "el_segments.json").write_text(
        json.dumps(el_segs or [], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print("[4/4] Write summary.txt")
    our_stats = stats_for_segments(our_dict)
    el_stats = stats_for_segments(el_segs) if el_segs else None
    write_summary(
        out_dir / "summary.txt",
        audio_path=audio_path,
        model=args.model,
        raw=raw,
        el_stats=el_stats,
        our_stats=our_stats,
    )

    if not args.keep_audio:
        try:
            audio_path.unlink()
        except OSError:
            pass

    print(f"\nDone. Inspect {out_dir}\\summary.txt")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="A/B test ElevenLabs segmented_json vs our heuristic.",
    )
    parser.add_argument("video", help="path to video / audio file")
    parser.add_argument(
        "--out",
        default="output/audit_segmented_json",
        help="output directory (default: output/audit_segmented_json)",
    )
    parser.add_argument(
        "--model",
        default="scribe_v2",
        choices=["scribe_v1", "scribe_v2"],
        help="STT model id (default: scribe_v2)",
    )
    parser.add_argument(
        "--language-code",
        default=None,
        help="optional ISO-639 language hint (e.g. 'eng', 'jpn', 'ind')",
    )
    parser.add_argument(
        "--no-verbatim",
        action="store_true",
        help="enable filler/false-start removal (scribe_v2 only)",
    )
    parser.add_argument(
        "--diarize",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="enable diarization (default: true)",
    )
    parser.add_argument(
        "--keyterms",
        nargs="*",
        default=None,
        help="optional keyterm bias list (incurs +20%% surcharge)",
    )
    parser.add_argument(
        "--keep-audio",
        action="store_true",
        help="keep the extracted .wav alongside the JSON outputs",
    )
    args = parser.parse_args()

    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
