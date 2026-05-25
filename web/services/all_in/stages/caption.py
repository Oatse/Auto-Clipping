"""
web.services.all_in.stages.caption — Auto-subtitle adapter.

Thin wrapper that takes a finished Clip MP4, builds a *synthetic*
``web.services.job_models.Job``, hands it to
``web.services.pipeline_runner.run_render_pipeline``, and returns
the captioned output path.

Why an adapter (not a full extraction): per ADR-0002, we ship the
All In Workspace by reusing the existing render pipeline as-is.
The pipeline is shaped around the ``Job`` model.  Building a
synthetic Job for a single Clip is ~30 lines of glue, vs. a
multi-day refactor to make the pipeline accept arbitrary inputs.

The synthetic Job is **disposable** — it never enters the
``_jobs`` dict in ``web.server`` and is not persisted to the
database.  It exists only for the lifetime of one Clip render so
the pipeline runner has the shape it expects.

Public API:
    burn_captions(clip_path, *, ...) -> Path
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Callable

from ..models import CaptionPreset
from ..presets import style_config_for

LogFn = Callable[[str], None]


class CaptionError(RuntimeError):
    """Raised when the auto-subtitle pipeline fails on a Clip.

    Wraps the underlying pipeline's ``job.error`` string so the
    runner can record it on the per-Clip ``AllInClip.error`` field.
    """


# ─── Public entry point ──────────────────────────────────────────────────────

async def burn_captions(
    *,
    clip_path: Path,
    output_dir: Path,
    caption_lang: str,
    preset: CaptionPreset | str,
    speaker_tinting: bool = False,
    log_fn: LogFn | None = None,
) -> Path:
    """Burn captions onto a single Clip and return the captioned MP4.

    The auto-subtitle pipeline is the same one driving the
    Auto-Subtitle Workspace — we feed it a synthetic Job pointed
    at ``clip_path``, let it run Phase 1-4, and pluck the output.

    Args:
        clip_path: The cut+silence-trimmed+reframed Clip on disk.
        output_dir: Where to land the captioned output.  The
            pipeline writes intermediate files under
            ``./output/{synthetic_job_id}/``; the final captioned
            video is copied to ``{output_dir}/{stem}_captioned.mp4``.
        caption_lang: Burned-in caption language (Q8 — separate from
            analysis_lang).  Drives Gemini translation if the Clip's
            spoken language differs.
        preset: Named style preset (Bold / Minimal / Karaoke).
        speaker_tinting: Forwarded to ``style_config`` so the
            renderer colour-codes captions by speaker (Q4).  No-op
            unless diarization data is present.
        log_fn: Optional log sink — adapted into the synthetic Job's
            ``log_lines`` so messages flow back to the All In SSE.

    Returns:
        Path to the captioned MP4.

    Raises:
        CaptionError: if the pipeline reports ``JobStatus.FAILED``
            or no output file is produced.
    """
    # Lazy imports — keeps the all_in package importable in
    # environments where the heavier render-pipeline deps aren't
    # available (model unit tests, etc.).
    from web.services.job_models import Job, JobStatus
    from web.services.pipeline_runner import run_render_pipeline

    # ── Build synthetic Job ────────────────────────────────────────────
    # The synthetic id is prefixed so it's visually distinguishable
    # from a real user-facing Job id in logs and on disk under
    # ./output/{synthetic_id}/.
    synthetic_id = f"ai{uuid.uuid4().hex[:10]}"
    synthetic_job = Job(
        id=synthetic_id,
        filename=clip_path.name,
        target_language=caption_lang,
        status=JobStatus.QUEUED,
        created_at=time.time(),
        video_path=str(clip_path),
        transcribe_only=False,
        # Speaker detection on by default — the renderer needs the
        # diarization data even when tinting is off, because karaoke
        # word-by-word highlighting still reads speaker IDs to keep
        # rendering deterministic.
        num_speakers=None,
        speaker_detection=True,
    )

    # Forward log_fn into the synthetic Job's log_lines so messages
    # flow back to the All In runner without a side channel.
    if log_fn:
        # The pipeline appends timestamped strings to log_lines;
        # bridge them into the All In log via a tail-watching closure.
        _bridge_logs(synthetic_job, log_fn)

    # ── Build style_config ─────────────────────────────────────────────
    style_config: dict = style_config_for(preset)
    if speaker_tinting:
        # The Q4 contract: tinted captions, most-words-wins overlap,
        # ``Hold`` on tie.  The subtitle renderer reads
        # ``speaker_tinting`` + ``speaker_overlap_policy`` from
        # style_config and resolves per-segment colour assignment.
        style_config["speaker_tinting"] = True
        style_config["speaker_overlap_policy"] = "most-words-wins"

    if log_fn:
        log_fn(f"Captioning {clip_path.name} (lang={caption_lang}, preset={preset})")

    # ── Run the pipeline ───────────────────────────────────────────────
    # ``run_render_pipeline`` mutates the Job in place; it never
    # raises (failures land on ``job.status`` / ``job.error``).
    await run_render_pipeline(
        synthetic_job,
        clip_path,
        caption_lang,
        style_config,
    )

    if synthetic_job.status == JobStatus.FAILED.value:
        raise CaptionError(
            synthetic_job.error or "Auto-subtitle pipeline reported FAILED"
        )

    if not synthetic_job.output_file:
        raise CaptionError(
            "Pipeline completed but produced no output_file"
        )

    pipeline_output = Path(synthetic_job.output_file)
    if not pipeline_output.exists():
        raise CaptionError(
            f"Pipeline reported output at {pipeline_output} but file is missing"
        )

    # ── Move output into the All In Job's directory ────────────────────
    # The pipeline writes under ./output/{synthetic_id}/.  We move
    # the final captioned MP4 next to the All In Job's other clips
    # so cleanup-on-Job-delete (Q12) can sweep one directory.
    output_dir.mkdir(parents=True, exist_ok=True)
    final_path = output_dir / f"{clip_path.stem}_captioned.mp4"
    _move_or_copy(pipeline_output, final_path)

    if log_fn:
        log_fn(f"Captions burned: {final_path.name}")

    return final_path


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _bridge_logs(synthetic_job, parent_log_fn: LogFn) -> None:
    """Tail the synthetic Job's log_lines into the parent log sink.

    The pipeline runner appends to ``synthetic_job.log_lines`` as it
    runs.  Wrapping the list with a subclass that fires a callback
    on append is the cleanest way to ferry messages back without
    polling.

    Caveat: this only works for ``.append``.  ``run_render_pipeline``
    only calls ``append``, so we accept the constraint.
    """
    original = synthetic_job.log_lines

    class _ForwardingList(list):
        def append(self, item):
            super().append(item)
            try:
                parent_log_fn(str(item))
            except Exception:  # noqa: BLE001 — parent sink failures
                # don't pollute the captioning path.
                pass

    forwarder = _ForwardingList(original)
    synthetic_job.log_lines = forwarder


def _move_or_copy(src: Path, dst: Path) -> None:
    """Atomically place ``src`` at ``dst``.

    Tries rename first (cheap, atomic on same filesystem), falls
    back to copy+unlink if cross-device link fails.  The All In
    Job directory and the synthetic pipeline directory may live
    on different drives on Windows.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        src.replace(dst)
    except OSError:
        dst.write_bytes(src.read_bytes())
        try:
            src.unlink()
        except OSError:
            pass


__all__ = ["CaptionError", "burn_captions"]
