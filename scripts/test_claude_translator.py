"""
scripts/test_claude_translator.py — Smoke-test the Claude translator backend.

Runs the same Auto-Subtitle Phase 1 (ElevenLabs STT + Phase 2 auto-translate)
flow that ``run_transcription_only`` runs in the web pipeline, but forces
``TRANSLATOR_BACKEND=claude`` for the duration of the script.

Why a script and not a pytest case:
  * The real test exercises live ElevenLabs + 9router endpoints — too
    chatty / costly for the regression suite.
  * The script registers a Job under ``output/<job_id>/`` and writes
    ``job_meta.json`` so the Job appears in the FastAPI Recent Jobs list
    after the next ``run_web.py`` start, exactly like a UI-driven Job.

Run it with the project venv::

    python scripts/test_claude_translator.py [path/to/video.mp4]

Defaults to ``output/newgrandma/MIKO(GRANDPA DANCE).mp4`` when no arg is given.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
import uuid
from pathlib import Path

# Force the Claude backend BEFORE any project module imports config so the
# selector picks it up. We restore the previous value at the end purely as
# a courtesy when the script is sourced from a parent process.
_PREV_BACKEND = os.environ.get("TRANSLATOR_BACKEND")
os.environ["TRANSLATOR_BACKEND"] = "claude"

# Make sure the project root is on sys.path when the script is invoked
# from a different working directory (e.g. via a worktree).
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Late imports so the env var above is in effect when config is loaded.
import config  # noqa: E402
from web.services import job_state  # noqa: E402
from web.services.job_models import Job, JobStatus  # noqa: E402
from web.services.pipeline_runner import run_transcription_only  # noqa: E402


DEFAULT_VIDEO = (
    PROJECT_ROOT / "output" / "newgrandma" / "MIKO(GRANDPA DANCE).mp4"
)
TARGET_LANGUAGE = "en"


def _print_banner(video_path: Path) -> None:
    print("=" * 72)
    print("Claude translator smoke test")
    print("=" * 72)
    print(f"Video           : {video_path}")
    print(f"Target language : {TARGET_LANGUAGE}")
    print(f"Backend         : {config.TRANSLATOR_BACKEND}")
    print(f"Claude model    : {config.TRANSLATOR_CLAUDE_MODEL}")
    print(f"9router URL     : {config.NINEROUTER_BASE_URL}")
    print(
        f"9router key set : "
        f"{'yes' if config.NINEROUTER_API_KEY else 'NO — script will skip translate'}"
    )
    print(
        f"ElevenLabs keys : "
        f"{len(config.ELEVENLABS_API_KEYS)} configured"
    )
    print("=" * 72)


def _stage_video(source: Path, job_id: str) -> Path:
    """Copy the source video into the uploads dir under the job id.

    Mirrors the FastAPI ``/api/jobs`` upload handler so the resulting Job
    has its ``video_path`` pointing under ``output/uploads/`` — that's the
    location ``delete_job`` expects, and ``get_video`` streams from.
    """
    uploads_dir: Path = job_state.UPLOADS_DIR
    uploads_dir.mkdir(parents=True, exist_ok=True)

    safe_name = source.name.replace(" ", "_").replace("(", "").replace(")", "")
    target = uploads_dir / f"{job_id}_{safe_name}"
    if target.exists():
        target.unlink()
    shutil.copy2(source, target)
    return target


async def _run_one(video_path: Path) -> Job:
    job_id = uuid.uuid4().hex[:12]

    print(f"\n[+] Staging video as Job {job_id}...")
    upload_path = _stage_video(video_path, job_id)
    print(f"    Uploaded to: {upload_path}")

    job = Job(
        id=job_id,
        filename=video_path.name,
        target_language=TARGET_LANGUAGE,
        status=JobStatus.QUEUED,
        created_at=time.time(),
        video_path=str(upload_path),
        transcribe_only=True,        # stop after Phase 2 auto-translate
        num_speakers=None,
        speaker_detection=True,
    )
    job_state.jobs[job_id] = job

    print(f"[+] Job registered. Logs will be mirrored to job.log_lines.")
    print(f"[+] Running Phase 1 (ElevenLabs STT) + Phase 2 (Claude translate)...\n")

    started = time.perf_counter()
    await run_transcription_only(job, upload_path, TARGET_LANGUAGE)
    elapsed = time.perf_counter() - started

    print("\n" + "=" * 72)
    print(f"Job {job_id} → status: {job.status}")
    print(f"Wall time      : {elapsed:.1f}s")
    print(f"Phase label    : {job.phase_label}")
    print(f"Transcript     : {job.transcript_path}")
    if job.error:
        print(f"Error          : {job.error}")
    print("=" * 72)
    return job


def _print_translated_sample(job: Job) -> None:
    """Show first few translated segments so the operator can eyeball quality."""
    if not job.transcript_path:
        return
    path = Path(job.transcript_path)
    if not path.exists():
        return

    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:  # noqa: BLE001 — best-effort sample
        print(f"[!] Could not read transcript for sample: {exc}")
        return

    segments = data.get("segments", []) if isinstance(data, dict) else data
    if not segments:
        print("[!] No segments in saved transcript.")
        return

    print(
        f"\nFirst {min(8, len(segments))} translated segments "
        f"(of {len(segments)}):\n"
    )
    for i, seg in enumerate(segments[:8]):
        start = seg.get("start", 0)
        end = seg.get("end", 0)
        text = (seg.get("text") or "").strip()
        print(f"  [{start:>6.2f}-{end:>6.2f}] {text}")


def _print_recent_jobs_hint(job: Job) -> None:
    """Tell the operator what to expect in the FastAPI Recent Jobs list."""
    print("\n" + "=" * 72)
    print("Recent Jobs visibility")
    print("=" * 72)
    print(
        "The Job has been registered in-memory and ``job_meta.json`` was "
        "persisted under output/<job_id>/. To see it in the web UI:"
    )
    print()
    print("  1. Start (or restart) the web server:")
    print("       python run_web.py")
    print()
    print(
        "  2. Open the Auto-Subtitle workspace — the restore handler scans "
        "output/ at startup and rehydrates jobs that have a saved "
        "transcript, so the new job will appear in 'Recent Jobs' with "
        "status='completed' (transcribe-only) and target_language='id'."
    )
    print()
    print(f"     Job ID: {job.id}")
    print(f"     Folder: output/{job.id}/")
    print("=" * 72)


async def _amain() -> int:
    if len(sys.argv) > 1:
        video_path = Path(sys.argv[1]).resolve()
    else:
        video_path = DEFAULT_VIDEO

    if not video_path.exists():
        print(f"[!] Video not found: {video_path}")
        return 1

    if not config.NINEROUTER_API_KEY:
        print(
            "[!] NINEROUTER_API_KEY is not set. Add it to .env before "
            "running this script — the Claude backend cannot translate "
            "without it."
        )
        return 2

    if not config.ELEVENLABS_API_KEYS:
        print(
            "[!] ELEVENLABS_API_KEY is not set. Phase 1 (STT) cannot run "
            "without it."
        )
        return 3

    _print_banner(video_path)

    job = await _run_one(video_path)

    if job.status == JobStatus.COMPLETED:
        _print_translated_sample(job)
        _print_recent_jobs_hint(job)
        return 0

    print(f"\n[!] Job did not complete successfully (status={job.status}).")
    if job.log_lines:
        print("\nLast 10 log lines:")
        for line in job.log_lines[-10:]:
            print(f"  {line}")
    return 4


def main() -> int:
    try:
        return asyncio.run(_amain())
    finally:
        # Restore TRANSLATOR_BACKEND to whatever the caller had set, just
        # in case the script is sourced from a long-lived shell.
        if _PREV_BACKEND is None:
            os.environ.pop("TRANSLATOR_BACKEND", None)
        else:
            os.environ["TRANSLATOR_BACKEND"] = _PREV_BACKEND


if __name__ == "__main__":
    raise SystemExit(main())
