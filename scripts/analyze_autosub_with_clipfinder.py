"""
scripts/analyze_autosub_with_clipfinder.py

Pakai ClipDetector untuk membaca transcript auto-sub YouTube (Bahasa Jepang
atau apa pun) dan ngeluarin daftar momen menarik (funny / goofy / sad /
desperate / laugh). Tiap momen dipadding 10-30s biar konteksnya kebawa.

Cara pakai:
    python scripts/analyze_autosub_with_clipfinder.py <path/to/transcript.json>

Output:
    - Cetak ringkasan ke stdout (judul + alasan + range waktu)
    - Tulis hasil mentah ke <path>.moments.json
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

# Pastikan repo root ada di sys.path supaya `import config` & `processors.*`
# bisa di-resolve waktu script dipanggil dari folder mana pun.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from processors.clip_finder.detector import ClipDetector
from processors.clip_finder.gemini_client import GeminiClient
from processors.clip_finder.heuristics import fmt_time

USER_INSTRUCTIONS = (
    "From start to finish, all moments like funny, goofy, relatable, "
    "exciting, sad, desperate, or any kind of laugh moment. and since "
    "this moment searching is done by text reading of the llm add like "
    "10-30s offset for each moment so we don't look any important context"
)


def _log(msg: str) -> None:
    # Pakai '-' bukan U+00B7 supaya aman di console cp932 (Windows JP locale).
    try:
        print(f"  - {msg}", flush=True)
    except UnicodeEncodeError:
        print(f"  - {msg.encode('ascii', 'replace').decode('ascii')}", flush=True)


async def _analyze(transcript_path: Path) -> int:
    # Force stdout/err ke UTF-8 supaya em-dash, kanji, dll. tidak mati
    # di console default Windows (cp932 / cp1252).
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if not transcript_path.is_file():
        print(f"[ERROR] File tidak ditemukan: {transcript_path}")
        return 2

    transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
    if not isinstance(transcript, list) or not transcript:
        print("[ERROR] Transcript JSON harus berupa array of segments.")
        return 2

    duration = max(seg["end"] for seg in transcript)
    print(f"Loaded {len(transcript)} segment(s), duration {fmt_time(duration)}")
    print(f"Memakai instruksi:\n  {USER_INSTRUCTIONS}\n")

    if not config.GEMINI_API_KEYS:
        print("[ERROR] GEMINI_API_KEYS kosong — set GEMINI_API_KEY_01 di .env")
        return 3

    client = GeminiClient(
        api_keys=config.GEMINI_API_KEYS,
        model=config.CLIP_FINDER_GEMINI_MODEL,
        fallback_models=config.CLIP_FINDER_GEMINI_FALLBACK_MODELS,
    )
    detector = ClipDetector(client)

    print("Memanggil Gemini (single-shot detect)...")
    candidates = await detector.detect(
        transcript=transcript,
        instructions=USER_INSTRUCTIONS,
        # Range ini mengakomodasi padding 10-30s yang diminta user
        # plus durasi inti momen pendek-sampai-sedang.
        min_clip=20.0,
        max_clip=120.0,
        video_duration=duration,
        signals=None,
        log_fn=_log,
    )

    if not candidates:
        print("\n[!] Gemini tidak menemukan momen apa pun.")
        return 0

    # Sort agar urutan ditampilkan kronologis.
    candidates.sort(key=lambda c: c.start)

    out_path = transcript_path.with_suffix(".moments.json")
    out_path.write_text(
        json.dumps([c.to_dict() for c in candidates], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"\nKetemu {len(candidates)} momen — raw JSON disimpan di {out_path}\n")
    print("=" * 78)
    for i, c in enumerate(candidates, 1):
        dur = c.end - c.start
        print(
            f"\n[{i:>2}] {fmt_time(c.start)} – {fmt_time(c.end)}  "
            f"({dur:.1f}s)  hunter={c.hunter.value}"
        )
        print(f"     Judul   : {c.title}")
        print(f"     Alasan  : {c.reason}")
    print("\n" + "=" * 78)
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    return asyncio.run(_analyze(Path(sys.argv[1]).resolve()))


if __name__ == "__main__":
    raise SystemExit(main())
