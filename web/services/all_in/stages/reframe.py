"""
web.services.all_in.stages.reframe — Aspect-ratio reframe stage.

Thin adapter over the extracted helpers in
``processors.short_maker``:

  - ``compute_smart_static_crop`` — single MediaPipe / OpenCV pass on
    20 evenly-spaced frames, median centroid, lock crop for the whole
    Clip.  Returns ``None`` for ratio == "original" or when no faces
    are detected.
  - ``reframe_to_ratio`` — applies the crop + scale via FFmpeg.

The stage's job is just to pick the right output filename, decide
when to skip (ratio == "original"), and translate stage-level errors
into the runner-friendly :class:`ReframeError`.

Public API:
    reframe_clip(input_path, ratio, output_dir, *, ...) -> Path
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from ..models import AspectRatio

LogFn = Callable[[str], None]


class ReframeError(RuntimeError):
    """Raised when reframe fails after the smart-static + centre-crop fallbacks.

    Distinct from FFmpeg crashes inside ``cut.py`` so the runner can
    decide per-stage retry policy.  In practice we don't auto-retry
    reframe — if face detection AND centre crop both fail, the Clip
    is marked FAILED and offered to the user via the retry button.
    """


# ─── Public entry point ──────────────────────────────────────────────────────

async def reframe_clip(
    *,
    input_path: Path,
    ratio: AspectRatio | str,
    output_dir: Path,
    clip_index: int,
    log_fn: LogFn | None = None,
) -> Path:
    """Reframe a single Clip to the requested aspect ratio.

    For ``AspectRatio.ORIGINAL`` returns ``input_path`` unchanged so
    the runner can skip the FFmpeg pass entirely (no copy, no encode,
    no transcode loss).

    For 9:16 / 1:1 calls into ``processors.short_maker`` for the crop
    compute + FFmpeg pass.  Output lands at
    ``{output_dir}/{stem}_reframed_{ratio}.mp4``.

    Errors from the underlying renderer are wrapped in
    :class:`ReframeError` so the runner doesn't have to know about
    FFmpeg specifics.
    """
    ratio_str = ratio.value if isinstance(ratio, AspectRatio) else str(ratio)

    if ratio_str == AspectRatio.ORIGINAL.value:
        if log_fn:
            log_fn("Reframe: ratio=original, skipping")
        return input_path

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / _reframed_filename(input_path, ratio_str)

    # Lazy import — keeps the all_in package importable in test
    # environments without OpenCV / NVENC FFmpeg installed.
    from processors.short_maker import reframe_to_ratio

    try:
        result = await reframe_to_ratio(
            input_path=input_path,
            output_path=output_path,
            ratio=ratio_str,
            crop=None,                  # let it compute smart-static
            log_fn=log_fn,
        )
    except FileNotFoundError as exc:
        raise ReframeError(f"Input file missing for reframe: {exc}") from exc
    except ValueError as exc:
        # _validate_crop in short_maker raises ValueError when the
        # computed crop exits the frame.  Surface as ReframeError so
        # the runner treats it as a per-Clip failure, not a Job-level
        # crash.
        raise ReframeError(f"Computed crop is invalid: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 — FFmpeg / OpenCV variance
        raise ReframeError(f"Reframe failed: {exc}") from exc

    if not result.exists():
        raise ReframeError(
            f"Reframe reported success but no file at {result}"
        )

    return result


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _reframed_filename(input_path: Path, ratio: str) -> str:
    """Build the reframed filename mirroring the cut-stage convention.

    e.g. ``clip_001_funny_bit.mp4`` + ``9:16`` →
    ``clip_001_funny_bit_reframed_9x16.mp4``.
    The colon in ``9:16`` is replaced with ``x`` so the path is
    valid on Windows.
    """
    safe_ratio = ratio.replace(":", "x").replace("/", "_")
    stem = input_path.stem
    return f"{stem}_reframed_{safe_ratio}.mp4"


__all__ = ["ReframeError", "reframe_clip"]
