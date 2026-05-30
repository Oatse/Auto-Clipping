"""
scripts/diag_claude_translate.py — Diagnose Claude translator issues.

Re-runs Phase 2 (TranslatorProcessor.translate) against an EXISTING
``source_transcript.json`` so we don't burn ElevenLabs quota while
debugging the Claude path.

Usage::

    python scripts/diag_claude_translate.py output/<job_id>

Outputs:
  * Raw HTTP probe of the 9router /v1/chat/completions endpoint.
  * Re-runs TranslatorProcessor with backend='claude' and prints the
    first 8 segments of the resulting translated_transcript.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

# Force backend BEFORE config loads so the selector picks it up.
os.environ["TRANSLATOR_BACKEND"] = "claude"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402
import httpx  # noqa: E402

from models.transcript import TranscriptSegment  # noqa: E402
from processors.translator import TranslatorProcessor  # noqa: E402


def _http_probe() -> None:
    """Confirm the 9router endpoint accepts our payload before any pipeline work."""
    print("=" * 72)
    print("HTTP probe: 9router /v1/chat/completions")
    print("=" * 72)
    url = config.NINEROUTER_BASE_URL.rstrip("/") + "/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {config.NINEROUTER_API_KEY}",
    }
    payload = {
        "model": config.TRANSLATOR_CLAUDE_MODEL,
        "messages": [
            {"role": "system", "content": "You output JSON arrays only."},
            {
                "role": "user",
                "content": (
                    "Translate these Japanese subtitles to English and "
                    "return ONLY a JSON array of strings:\n\n"
                    "1. こんにちは\n"
                    "2. ありがとう\n"
                    "3. 元気ですか"
                ),
            },
        ],
        "temperature": 0.2,
        "max_tokens": 1024,
    }
    print(f"URL    : {url}")
    print(f"Model  : {payload['model']}")
    print(f"Key set: {'yes' if config.NINEROUTER_API_KEY else 'NO'}")

    try:
        r = httpx.post(url, json=payload, headers=headers, timeout=120.0)
    except Exception as exc:  # noqa: BLE001
        print(f"[!] Network error: {exc}")
        return

    print(f"HTTP {r.status_code}")
    print(f"Content-Type : {r.headers.get('content-type')}")
    print(f"Body length  : {len(r.text)}")
    print(f"Body (first 800 chars):\n{r.text[:800]}\n")
    if r.status_code != 200:
        return

    try:
        data = r.json()
    except Exception as exc:  # noqa: BLE001
        print(f"[!] Body is not JSON: {exc}")
        return
    print("Top-level keys:", list(data.keys()))
    choices = data.get("choices", [])
    if not choices:
        print("[!] No choices in response.")
        print(json.dumps(data, indent=2)[:1500])
        return
    msg = choices[0].get("message", {})
    print("Choice 0 finish_reason:", choices[0].get("finish_reason"))
    print("Choice 0 message keys :", list(msg.keys()))
    content = msg.get("content", "")
    print(f"Content (first 600 chars):\n{content[:600]}")


async def _rerun_translate(job_dir: Path, *, promote: bool = False) -> None:
    transcript_path = job_dir / "phase1_transcription" / "source_transcript.json"
    if not transcript_path.exists():
        print(f"[!] No source_transcript.json under {job_dir}")
        return

    print("\n" + "=" * 72)
    print(f"Re-running TranslatorProcessor against {transcript_path}")
    print("=" * 72)
    with transcript_path.open("r", encoding="utf-8") as f:
        raw = json.load(f)

    raw_segs = raw.get("segments", []) if isinstance(raw, dict) else raw
    segments = [TranscriptSegment.from_dict(s) for s in raw_segs]
    print(f"Loaded {len(segments)} segments from disk.")

    # When ``promote`` is True we overwrite the canonical phase2_translation/
    # folder so the result is what the FastAPI Recent Jobs list will surface.
    # Otherwise write to a sidecar so the original output stays untouched.
    sub = "phase2_translation" if promote else "phase2_translation_claude_rerun"
    out_dir = job_dir / sub
    out_dir.mkdir(parents=True, exist_ok=True)

    translator = TranslatorProcessor(
        target_language="en",
        backend="claude",
    )
    print(f"Translator backend: {translator.backend}")
    translated, json_path = await translator.translate(
        segments=segments,
        output_dir=out_dir,
        regroup=True,
    )

    print(f"\n→ Wrote {json_path}")
    print(f"  Total translated segments: {len(translated)}")
    print("\nFirst 12 segments:")
    for i, seg in enumerate(translated[:12]):
        text = seg.text.strip()
        print(f"  [{seg.start:6.2f}-{seg.end:6.2f}] {text}")

    if promote:
        # Refresh job_meta.json so the FastAPI restore handler picks up the
        # completion timestamp and target language correctly.
        meta_path = job_dir / "job_meta.json"
        meta: dict = {}
        if meta_path.exists():
            try:
                with meta_path.open("r", encoding="utf-8") as f:
                    meta = json.load(f)
            except Exception:  # noqa: BLE001
                meta = {}
        import time as _t
        meta["status"] = "completed"
        meta["target_language"] = "en"
        meta["transcribe_only"] = True
        meta["completed_at"] = _t.time()
        meta["phase_label"] = "Transcription complete — Ready for preview"
        meta["progress_pct"] = 25.0
        meta["current_phase"] = 1
        meta["error"] = None
        with meta_path.open("w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        print(f"\n[+] Promoted to canonical phase2 + refreshed {meta_path}")


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    promote = "--promote" in sys.argv[1:]

    if args:
        job_dir = Path(args[0]).resolve()
    else:
        # Default to the most recent job from the previous smoke test.
        candidates = sorted(
            (PROJECT_ROOT / "output").glob("[0-9a-f]" * 12),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        candidates = [
            p for p in candidates
            if (p / "phase1_transcription" / "source_transcript.json").exists()
        ]
        if not candidates:
            print("[!] No job folder with phase1_transcription/source_transcript.json found.")
            return 2
        job_dir = candidates[0]
        print(f"[+] Using most recent job dir: {job_dir.name}")

    if promote:
        print("[+] --promote: writing into canonical phase2_translation/ folder")

    _http_probe()
    asyncio.run(_rerun_translate(job_dir, promote=promote))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
