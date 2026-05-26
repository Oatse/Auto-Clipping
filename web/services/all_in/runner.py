"""
web.services.all_in.runner — Top-level orchestrator for All In Jobs.

Composes the stage modules into the full pipeline:

    download source → detect moments → for each Moment:
        cut → tighten silence → reframe → caption → mark DONE

Per design grilling decisions:

  - Q2  one-shot run, Clip Cards stream as they finish
  - Q10 per-Clip status with retry endpoint
  - Q11 serial single worker (no parallel renders)
  - Q12 source persists with the Job; deleted only on Job delete

Public API:
  - run_all_in_job(job, *, output_root, ...)
  - retry_clip(job, clip_idx, *, output_root, ...)

Both coroutines mutate the supplied :class:`AllInJob` in place
(status, progress, log_lines, per-Clip status) and never raise —
failures land on ``job.error`` for Job-level fatal errors and on
``clip.error`` for per-Clip failures.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Callable

from loguru import logger

import config

from .models import (
    AllInClip,
    AllInClipStatus,
    AllInJob,
    AllInJobStatus,
    AspectRatio,
    CaptionPreset,
    CutStrategyChoice,
    DetectionMode,
    ScoringProfileChoice,
)
from .stages.cut import CutError, cut_moment, tighten_silence
from .stages.caption import CaptionError, burn_captions
from .stages.moments import (
    NoMomentsFoundError,
    TranscriptUnavailableError,
    detect_moments,
)
from .stages.reframe import ReframeError, reframe_clip
from .stages.source import SourceDownloadError, download_source

LogFn = Callable[[str], None]


# ─── Loggers / persistence helpers ───────────────────────────────────────────

def _make_logger(job: AllInJob) -> LogFn:
    """Return a closure that appends to ``job.log_lines`` and loguru."""
    def log(msg: str) -> None:
        job.log_lines.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        logger.info("[AllIn {}] {}", job.id[:8], msg)
    return log


def _persist_job_meta(job: AllInJob, job_dir: Path) -> None:
    """Write ``job_meta.json`` so the FastAPI restore handler can rehydrate."""
    try:
        job_dir.mkdir(parents=True, exist_ok=True)
        meta_path = job_dir / "job_meta.json"
        with meta_path.open("w", encoding="utf-8") as f:
            json.dump(
                job.model_dump(exclude={"log_lines", "transcript"}),
                f, ensure_ascii=False, indent=2,
            )
    except Exception as exc:  # noqa: BLE001 — meta is best-effort.
        logger.warning("[AllIn {}] meta save failed: {}", job.id[:8], exc)


def _job_dir(job: AllInJob, output_root: Path) -> Path:
    """Per-Job directory.  Source video, clips, and meta all live here."""
    return output_root / job.id


def _clips_dir(job: AllInJob, output_root: Path) -> Path:
    return _job_dir(job, output_root) / "clips"


# ─── Per-Clip render loop ────────────────────────────────────────────────────

async def _write_clip_sidecar(
    *,
    job: AllInJob,
    clip: AllInClip,
    clip_path: Path,
    log: LogFn,
) -> None:
    """Generate + write the Clip Sidecar (CONTEXT.md: "Clip Sidecar").

    Wraps ``processors.clip_finder.clip_sidecar.generate`` with the
    per-Clip transcript window and the Job's API keys. Failure is
    contained inside this helper — the caller treats it as best-effort.
    """
    # Lazy import — keeps the all_in package importable in tests that
    # don't pull in the full clip_finder deps.
    from processors.clip_finder import clip_sidecar as _sidecar

    # Slice the Job's transcript to just the words inside [start, end].
    window = [
        seg for seg in (job.transcript or [])
        if isinstance(seg, dict)
        and float(seg.get("end", 0.0)) >= clip.start
        and float(seg.get("start", 0.0)) <= clip.end
    ]

    api_keys = list(getattr(config, "GEMINI_API_KEYS", []) or [])
    sidecar = await _sidecar.generate(
        clip_title=clip.title,
        clip_reason=clip.reason,
        clip_duration=max(0.0, clip.end - clip.start),
        transcript_window=window,
        api_keys=api_keys,
        gemini_model=getattr(
            config, "CLIP_FINDER_GEMINI_MODEL", "gemini-3.5-flash",
        ),
        fallback_models=getattr(
            config, "CLIP_FINDER_GEMINI_FALLBACK_MODELS", [],
        ),
        log_fn=log,
    )
    written = _sidecar.write(sidecar, clip_path)
    log(f"Clip {clip.index + 1}: sidecar → {written.name}")


async def _render_clip(
    *,
    job: AllInJob,
    clip: AllInClip,
    source: Path,
    output_root: Path,
    log: LogFn,
) -> None:
    """Run one Clip through cut → silence-trim → reframe → caption.

    Mutates ``clip.status`` / ``clip.stage_label`` / ``clip.clip_file``
    / ``clip.error`` in place.  Never raises — every failure is
    recorded on the Clip and the loop moves on (Q10 per-Clip
    independence).
    """
    clips_root = _clips_dir(job, output_root)
    work_dir = clips_root / f"clip_{clip.index:03d}"
    work_dir.mkdir(parents=True, exist_ok=True)

    clip.status = AllInClipStatus.RENDERING
    clip.error = None

    try:
        # ── Stage 1: range cut ─────────────────────────────────────────
        clip.stage_label = "Cutting"
        log(f"Clip {clip.index + 1}: cutting {clip.start:.2f}s → {clip.end:.2f}s")
        cut_path = await cut_moment(
            source=source,
            start=clip.start,
            end=clip.end,
            output_dir=work_dir,
            clip_index=clip.index,
            title=clip.title,
            ffmpeg_path=getattr(config, "FFMPEG_PATH", "ffmpeg"),
            log_fn=log,
        )

        # ── Stage 2: silence trim (optional, default ON per Q6) ────────
        if job.tighten_silence:
            clip.stage_label = "Tightening silence"
            trimmed_path = work_dir / f"{cut_path.stem}_trimmed.mp4"
            current_path = await tighten_silence(
                input_path=cut_path,
                output_path=trimmed_path,
                ffmpeg_path=getattr(config, "FFMPEG_PATH", "ffmpeg"),
                log_fn=log,
            )
        else:
            current_path = cut_path

        # ── Stage 3: reframe ───────────────────────────────────────────
        clip.stage_label = "Reframing"
        ratio = job.aspect_ratio
        if isinstance(ratio, str):
            ratio = AspectRatio(ratio)
        current_path = await reframe_clip(
            input_path=current_path,
            ratio=ratio,
            output_dir=work_dir,
            clip_index=clip.index,
            log_fn=log,
        )

        # ── Stage 4: auto-subtitle (optional, default ON per Q7) ───────
        if job.auto_subtitle:
            clip.stage_label = "Captioning"
            preset = job.caption_preset
            if isinstance(preset, str):
                preset = CaptionPreset(preset)
            current_path = await burn_captions(
                clip_path=current_path,
                output_dir=work_dir,
                caption_lang=job.caption_lang,
                preset=preset,
                speaker_tinting=job.speaker_tinting,
                log_fn=log,
            )

        # ── Stage 5: Clip Sidecar (upload-ready metadata) ──────────────
        # Best-effort: generate a metadata.json next to the finished Clip
        # so the user can copy-paste title/description/hashtags. Failure
        # never affects the Clip — generate() returns a default sidecar
        # on any error path.
        try:
            await _write_clip_sidecar(
                job=job,
                clip=clip,
                clip_path=current_path,
                log=log,
            )
        except Exception as exc:  # noqa: BLE001 — sidecar is opportunistic.
            log(f"Clip {clip.index + 1}: sidecar skipped — {exc}")

        # ── Done ───────────────────────────────────────────────────────
        clip.clip_file = str(current_path)
        clip.stage_label = None
        clip.status = AllInClipStatus.DONE
        log(f"Clip {clip.index + 1}: done → {Path(current_path).name}")

    except (CutError, ReframeError, CaptionError) as exc:
        # Stage-typed failure — record as per-Clip FAILED and keep
        # the Job going.  The user can hit retry to re-enter this
        # function with the same Clip.
        clip.status = AllInClipStatus.FAILED
        clip.error = str(exc)
        clip.stage_label = None
        log(f"Clip {clip.index + 1}: FAILED — {exc}")
    except asyncio.CancelledError:
        # Propagate cancellation so the runner can mark the Job
        # CANCELLED cleanly.
        clip.status = AllInClipStatus.FAILED
        clip.error = "Cancelled"
        raise
    except Exception as exc:  # noqa: BLE001 — last-resort safety net.
        clip.status = AllInClipStatus.FAILED
        clip.error = f"Unexpected error: {exc}"
        clip.stage_label = None
        log(f"Clip {clip.index + 1}: FAILED — {type(exc).__name__}: {exc}")


# ─── Per-Clip retry (public) ─────────────────────────────────────────────────

async def retry_clip(
    job: AllInJob,
    clip_idx: int,
    *,
    output_root: Path,
) -> None:
    """Re-run the per-Clip render loop for a single failed Clip.

    Re-uses the source video already on disk (Q12) so retry is
    cheap.  Raises ``IndexError`` if ``clip_idx`` is out of range
    and ``RuntimeError`` if the source was manually removed.

    Mutates the Job:
      - flips the Clip status to RENDERING then DONE/FAILED
      - bumps the Job back to RENDERING for the duration if it had
        already been COMPLETED
      - re-evaluates terminal status at the end
    """
    if clip_idx < 0 or clip_idx >= len(job.clips):
        raise IndexError(f"clip_idx {clip_idx} out of range (have {len(job.clips)})")

    if not job.source_path:
        raise RuntimeError("Job has no source_path — cannot retry without source")

    source = Path(job.source_path)
    if not source.exists():
        raise RuntimeError(
            f"Source file missing at {source} — re-run the full Job"
        )

    log = _make_logger(job)
    log(f"Retry: clip {clip_idx + 1}")

    # Restore RENDERING on the Job so the UI knows work is in flight.
    previous_status = job.status
    job.status = AllInJobStatus.RENDERING

    try:
        await _render_clip(
            job=job,
            clip=job.clips[clip_idx],
            source=source,
            output_root=output_root,
            log=log,
        )
    finally:
        # Re-evaluate terminal status.
        if job.is_terminal():
            job.status = AllInJobStatus.COMPLETED
            job.progress_pct = 100.0
            job.phase_label = (
                f"Completed — {job.done_count()} done, "
                f"{job.failed_count()} failed"
            )
            job.completed_at = time.time()
        else:
            job.status = previous_status

        _persist_job_meta(job, _job_dir(job, output_root))


# ─── Top-level Job orchestrator (public) ─────────────────────────────────────

async def run_all_in_job(
    job: AllInJob,
    *,
    output_root: Path,
    gemini_keys: list[str],
) -> None:
    """Run an All In Job end-to-end.

    Stages:
      1. ``DOWNLOADING`` — full source video to ``{job_dir}/source.mp4``.
      2. ``ANALYZING``   — transcript + signals + Gemini moments.
      3. ``RENDERING``   — per-Clip serial loop (Q11).
      4. ``COMPLETED`` once every Clip is in a terminal state.

    Mutates the Job in place and never raises.  Job-level fatal
    errors land on ``job.status = FAILED`` + ``job.error``.  Per-Clip
    failures stay on the individual ``AllInClip.error`` slots; the
    Job still reaches ``COMPLETED`` even with some failed Clips
    (Q10 contract).
    """
    job_dir = _job_dir(job, output_root)
    job_dir.mkdir(parents=True, exist_ok=True)

    log = _make_logger(job)
    log(f"All In Job started: {job.url}")

    try:
        # ── Step 1: download source ────────────────────────────────────
        job.status = AllInJobStatus.DOWNLOADING
        job.phase_label = "Downloading source video"
        job.progress_pct = 5.0
        _persist_job_meta(job, job_dir)

        try:
            source_video = await download_source(
                url=job.url,
                output_dir=job_dir,
                cookies_file=getattr(config, "YTDLP_COOKIES_FILE", ""),
                cookies_browser=getattr(config, "YTDLP_COOKIES_BROWSER", ""),
                log_fn=log,
            )
        except SourceDownloadError as exc:
            job.status = AllInJobStatus.FAILED
            job.error = str(exc)
            job.phase_label = "Failed — source download"
            job.completed_at = time.time()
            _persist_job_meta(job, job_dir)
            return

        job.source_path = str(source_video.path)
        job.source_title = source_video.title

        # ── Step 2: detect moments ─────────────────────────────────────
        job.status = AllInJobStatus.ANALYZING
        job.phase_label = "Analyzing transcript with Gemini"
        job.progress_pct = 25.0
        _persist_job_meta(job, job_dir)

        try:
            mode = job.mode
            if isinstance(mode, str):
                mode = DetectionMode(mode)
            # ADR-0003: forward Scoring Profile + Cut Strategies through to
            # the detection stage so candidates are scored correctly and
            # variants fan out before render. Empty cut_strategies preserves
            # legacy 1 Moment → 1 Clip behaviour.
            profile_value = (
                job.scoring_profile.value
                if isinstance(job.scoring_profile, ScoringProfileChoice)
                else str(job.scoring_profile or "vtuber")
            )
            strategy_values = [
                (s.value if isinstance(s, CutStrategyChoice) else str(s))
                for s in (job.cut_strategies or [])
            ]
            result = await detect_moments(
                url=job.url,
                instructions=job.instructions,
                job_dir=job_dir,
                analysis_lang=job.analysis_lang,
                mode=mode,
                enable_audio_signals=job.enable_audio_signals,
                enable_chat_signals=job.enable_chat_signals,
                start_offset=job.start_offset,
                max_clips=job.max_clips,
                gemini_keys=gemini_keys,
                cookies_file=getattr(config, "YTDLP_COOKIES_FILE", ""),
                cookies_browser=getattr(config, "YTDLP_COOKIES_BROWSER", ""),
                gemini_model=getattr(
                    config, "CLIP_FINDER_GEMINI_MODEL", "gemini-3.5-flash",
                ),
                cache_dir=getattr(config, "CLIP_FINDER_CACHE_DIR", None),
                ffmpeg_path=getattr(config, "FFMPEG_PATH", "ffmpeg"),
                scoring_profile=profile_value,
                cut_strategies=strategy_values,
                # All In owns the source on disk (ADR-0002 Q12) — pass it
                # through so visual_signals can extract scene cuts without
                # a re-download.
                source_video_path=source_video.path,
                enable_visual_signals=True,
                log_fn=log,
            )
        except TranscriptUnavailableError as exc:
            # Recoverable — Job is COMPLETED with zero Clips, not FAILED.
            job.status = AllInJobStatus.COMPLETED
            job.error = str(exc)
            job.phase_label = "No transcript available"
            job.progress_pct = 100.0
            job.completed_at = time.time()
            _persist_job_meta(job, job_dir)
            return
        except NoMomentsFoundError:
            job.status = AllInJobStatus.COMPLETED
            job.phase_label = "No matching moments"
            job.progress_pct = 100.0
            job.completed_at = time.time()
            _persist_job_meta(job, job_dir)
            log("Gemini returned zero moments. Try refining instructions.")
            return

        job.clips = result.clips
        job.transcript = result.transcript
        job.signals_summary = result.signals_summary

        log(f"Analysis complete — rendering {len(job.clips)} clip(s)")
        _persist_job_meta(job, job_dir)

        # ── Step 3: per-Clip render loop (serial — Q11) ────────────────
        job.status = AllInJobStatus.RENDERING
        for i, clip in enumerate(job.clips):
            # Recompute progress as clips finish so the UI bar moves
            # smoothly through the rendering window (35% → 100%).
            done_so_far = sum(
                1 for c in job.clips
                if c.status in (
                    AllInClipStatus.DONE.value,
                    AllInClipStatus.FAILED.value,
                )
            )
            job.progress_pct = 35.0 + (done_so_far / len(job.clips)) * 60.0
            job.phase_label = f"Rendering clip {i + 1}/{len(job.clips)}"

            await _render_clip(
                job=job,
                clip=clip,
                source=source_video.path,
                output_root=output_root,
                log=log,
            )
            _persist_job_meta(job, job_dir)

        # ── Step 4: terminal status ────────────────────────────────────
        job.status = AllInJobStatus.COMPLETED
        job.progress_pct = 100.0
        job.phase_label = (
            f"Completed — {job.done_count()} done, {job.failed_count()} failed"
        )
        job.completed_at = time.time()
        _persist_job_meta(job, job_dir)
        log(job.phase_label)

    except asyncio.CancelledError:
        job.status = AllInJobStatus.CANCELLED
        job.phase_label = "Cancelled"
        job.completed_at = time.time()
        _persist_job_meta(job, job_dir)
        raise
    except Exception as exc:  # noqa: BLE001 — last-resort.
        job.status = AllInJobStatus.FAILED
        job.error = f"Unexpected: {type(exc).__name__}: {exc}"
        job.phase_label = "Failed — unexpected error"
        job.completed_at = time.time()
        _persist_job_meta(job, job_dir)
        logger.exception("[AllIn {}] unexpected failure", job.id[:8])


__all__ = ["run_all_in_job", "retry_clip"]
