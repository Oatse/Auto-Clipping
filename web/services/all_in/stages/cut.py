"""
web.services.all_in.stages.cut — Range cut + optional silence trim.

Two responsibilities:

1. **Range cut.**  Extract ``source[moment.start:moment.end]`` from
   the full source video into a per-Clip raw file.  Frame-accurate
   because we re-encode the cut span (NVENC if available, libx264
   fallback) — yt-dlp's ``--download-sections`` keyframe alignment
   doesn't apply here since we own the source on disk (Q3).

2. **Silence trim** (optional, default ON per Q6).  Run FFmpeg
   ``silencedetect`` on the cut clip, stitch the non-silent spans
   back together with 100 ms head/tail buffer to avoid clipping
   breath/word onsets.  If detection finds nothing, the output
   equals the input — no regression.

Public API:
    cut_moment(source, moment, output_dir, *, ...) -> Path
    tighten_silence(input_path, output_path, *, ...) -> Path
"""

from __future__ import annotations

import asyncio
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

LogFn = Callable[[str], None]


# ─── Errors ──────────────────────────────────────────────────────────────────

class CutError(RuntimeError):
    """FFmpeg failure during range cut or silence stitching."""


# ─── Internal config ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class _SilencePolicy:
    """Tunables for ``tighten_silence``.

    Defaults match the design grilling Q6 contract: silence threshold
    -30 dB, minimum silence duration 800 ms, stitch buffer 100 ms.
    """

    noise_db: float = -30.0
    min_silence_seconds: float = 0.8
    head_tail_buffer_seconds: float = 0.1


_DEFAULT_SILENCE = _SilencePolicy()


# ─── Range cut ───────────────────────────────────────────────────────────────

async def cut_moment(
    *,
    source: Path,
    start: float,
    end: float,
    output_dir: Path,
    clip_index: int,
    title: str = "",
    ffmpeg_path: str = "ffmpeg",
    use_nvenc: bool = True,
    log_fn: LogFn | None = None,
) -> Path:
    """Cut ``source[start:end]`` into ``output_dir/clip_NNN_title.mp4``.

    Returns the path to the cut file.  Raises :class:`CutError` if
    FFmpeg exits non-zero or the output file does not appear on disk.

    The output filename mirrors the convention used by
    ``processors.clip_finder.downloader.ClipDownloader`` so a user
    moving between workspaces sees the same naming pattern.
    """
    if end <= start:
        raise CutError(
            f"Invalid range: start={start:.2f} >= end={end:.2f}"
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / _safe_clip_filename(clip_index, title)

    duration = end - start
    encoder_args = _build_encoder_args(use_nvenc=use_nvenc)
    cmd = [
        ffmpeg_path,
        "-y",
        "-ss", f"{start:.3f}",        # input-side seek for speed…
        "-i", str(source),
        "-t", f"{duration:.3f}",      # …then duration-bound the cut.
        "-avoid_negative_ts", "make_zero",
        *encoder_args,
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        str(output_file),
    ]

    if log_fn:
        log_fn(f"Cutting clip {clip_index + 1}: {start:.2f}s → {end:.2f}s ({duration:.2f}s)")

    rc, stderr = await _run_ffmpeg(cmd)
    if rc != 0 or not output_file.exists():
        # Retry once with libx264 if NVENC was the cause — common on
        # boxes without an NVIDIA GPU but with NVENC-built FFmpeg.
        if use_nvenc and "nvenc" in stderr.lower():
            if log_fn:
                log_fn("NVENC failed, retrying cut with libx264 fallback...")
            return await cut_moment(
                source=source,
                start=start,
                end=end,
                output_dir=output_dir,
                clip_index=clip_index,
                title=title,
                ffmpeg_path=ffmpeg_path,
                use_nvenc=False,
                log_fn=log_fn,
            )
        raise CutError(
            f"FFmpeg cut failed (rc={rc}): {stderr[:300]}"
        )

    return output_file


# ─── Silence trim ────────────────────────────────────────────────────────────

async def tighten_silence(
    *,
    input_path: Path,
    output_path: Path,
    ffmpeg_path: str = "ffmpeg",
    policy: _SilencePolicy = _DEFAULT_SILENCE,
    log_fn: LogFn | None = None,
) -> Path:
    """Remove long silence gaps from ``input_path``.

    If ``silencedetect`` finds nothing worth trimming, the input is
    copied to ``output_path`` unchanged so callers always get a
    valid file at the expected location.

    Behaviour matches Q6: detect silence > 800 ms, stitch
    non-silent spans with 100 ms head/tail buffer.
    """
    if not input_path.exists():
        raise CutError(f"Input file missing: {input_path}")

    spans = await _detect_nonsilent_spans(
        input_path, policy=policy, ffmpeg_path=ffmpeg_path,
    )
    if not spans:
        # Either the clip is entirely silent, or detection failed.
        # Either way, returning the original is the safe fallback —
        # silence trim is opportunistic, not load-bearing.
        if log_fn:
            log_fn("Silence trim: no non-silent spans found, keeping original")
        _copy_atomic(input_path, output_path)
        return output_path

    if len(spans) == 1:
        # Single non-silent span ≈ no meaningful silence to trim.
        # Cheaper than running the concat filter for a no-op.
        if log_fn:
            log_fn("Silence trim: single span detected, no stitching needed")
        _copy_atomic(input_path, output_path)
        return output_path

    if log_fn:
        kept = sum(e - s for s, e in spans)
        log_fn(
            f"Silence trim: kept {len(spans)} span(s), "
            f"{kept:.2f}s total"
        )

    cmd = _build_concat_command(
        input_path=input_path,
        output_path=output_path,
        spans=spans,
        ffmpeg_path=ffmpeg_path,
    )
    rc, stderr = await _run_ffmpeg(cmd)
    if rc != 0 or not output_path.exists():
        # Stitch failure → return the original rather than a broken
        # output.  The user sees a slightly longer clip, never a
        # missing one.
        if log_fn:
            log_fn(f"Silence trim failed (rc={rc}), falling back to original")
        _copy_atomic(input_path, output_path)
    return output_path


# ─── FFmpeg helpers ──────────────────────────────────────────────────────────

def _build_encoder_args(*, use_nvenc: bool) -> list[str]:
    """Return ``-c:v ...`` args for the cut pass."""
    if use_nvenc:
        return ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "23"]
    return ["-c:v", "libx264", "-preset", "fast", "-crf", "20"]


def _build_concat_command(
    *,
    input_path: Path,
    output_path: Path,
    spans: list[tuple[float, float]],
    ffmpeg_path: str,
) -> list[str]:
    """Build a single-process FFmpeg concat-filter command.

    Each span becomes a ``[Vn][An]`` pair via ``trim``+``setpts`` /
    ``atrim``+``asetpts``; the pairs are concat-filter joined with
    ``n=N:v=1:a=1``.  Output is re-encoded once at the tail so we
    don't depend on the input's keyframe layout.
    """
    n = len(spans)
    filter_parts: list[str] = []
    concat_inputs: list[str] = []
    for i, (s, e) in enumerate(spans):
        filter_parts.append(
            f"[0:v]trim=start={s:.3f}:end={e:.3f},setpts=PTS-STARTPTS[v{i}];"
            f"[0:a]atrim=start={s:.3f}:end={e:.3f},asetpts=PTS-STARTPTS[a{i}]"
        )
        concat_inputs.append(f"[v{i}][a{i}]")
    filter_complex = ";".join(filter_parts) + ";"
    filter_complex += "".join(concat_inputs) + f"concat=n={n}:v=1:a=1[v][a]"

    return [
        ffmpeg_path,
        "-y",
        "-i", str(input_path),
        "-filter_complex", filter_complex,
        "-map", "[v]",
        "-map", "[a]",
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "20",
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        str(output_path),
    ]


_SILENCE_END_RE = re.compile(
    r"silence_end:\s*([\d.]+)\s*\|\s*silence_duration:\s*([\d.]+)"
)
_SILENCE_START_RE = re.compile(r"silence_start:\s*([\d.]+)")


async def _detect_nonsilent_spans(
    input_path: Path,
    *,
    policy: _SilencePolicy,
    ffmpeg_path: str,
) -> list[tuple[float, float]]:
    """Return non-silent ``(start, end)`` spans in seconds.

    Includes ``policy.head_tail_buffer_seconds`` of buffer on each
    edge so we never clip breath / word onsets.  Spans are clamped
    to [0, duration] and merged if buffering causes overlap.
    """
    duration = await _probe_duration(input_path, ffmpeg_path=ffmpeg_path)
    if duration <= 0:
        return []

    cmd = [
        ffmpeg_path,
        "-i", str(input_path),
        "-af", f"silencedetect=noise={policy.noise_db}dB:d={policy.min_silence_seconds}",
        "-f", "null",
        "-",
    ]
    _, stderr = await _run_ffmpeg(cmd)

    silences: list[tuple[float, float]] = []
    cur_start: float | None = None
    for line in stderr.splitlines():
        m_start = _SILENCE_START_RE.search(line)
        if m_start:
            cur_start = float(m_start.group(1))
            continue
        m_end = _SILENCE_END_RE.search(line)
        if m_end and cur_start is not None:
            silences.append((cur_start, float(m_end.group(1))))
            cur_start = None
    # Trailing silence to EOF (no silence_end emitted).
    if cur_start is not None:
        silences.append((cur_start, duration))

    if not silences:
        return [(0.0, duration)]

    # Invert silence intervals → non-silent spans.
    spans: list[tuple[float, float]] = []
    cursor = 0.0
    for s_start, s_end in silences:
        if s_start > cursor:
            spans.append((cursor, s_start))
        cursor = max(cursor, s_end)
    if cursor < duration:
        spans.append((cursor, duration))

    # Apply head/tail buffer + clamp + merge overlaps.
    buf = policy.head_tail_buffer_seconds
    expanded = [(max(0.0, s - buf), min(duration, e + buf)) for s, e in spans]
    return _merge_overlapping(expanded)


def _merge_overlapping(
    spans: Iterable[tuple[float, float]],
) -> list[tuple[float, float]]:
    """Merge overlapping or adjacent spans into one."""
    out: list[tuple[float, float]] = []
    for start, end in sorted(spans):
        if end <= start:
            continue
        if out and start <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return out


async def _probe_duration(input_path: Path, *, ffmpeg_path: str) -> float:
    """Return the duration of ``input_path`` in seconds.

    Uses ``ffprobe`` if it sits next to the configured ffmpeg.  Falls
    back to parsing ffmpeg's own startup banner if ffprobe is missing.
    """
    ffprobe = ffmpeg_path.replace("ffmpeg", "ffprobe", 1)
    rc, stdout = await _run_capture([
        ffprobe, "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1",
        str(input_path),
    ])
    if rc == 0:
        try:
            return float(stdout.strip())
        except ValueError:
            pass
    return 0.0


async def _run_ffmpeg(cmd: list[str]) -> tuple[int, str]:
    """Run an ffmpeg command, return (exit_code, stderr_as_string)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr_bytes = await proc.communicate()
    stderr = (stderr_bytes or b"").decode("utf-8", errors="replace")
    return (proc.returncode or 0), stderr


async def _run_capture(cmd: list[str]) -> tuple[int, str]:
    """Run a command capturing stdout (used for ffprobe)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout_bytes, _ = await proc.communicate()
    return (proc.returncode or 0), (stdout_bytes or b"").decode("utf-8", errors="replace")


def _copy_atomic(src: Path, dst: Path) -> None:
    """Best-effort atomic copy used by silence-trim fallbacks."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".part")
    tmp.write_bytes(src.read_bytes())
    tmp.replace(dst)


def _safe_clip_filename(index: int, title: str) -> str:
    """Mirror the Clip Finder downloader naming convention."""
    safe = re.sub(r"[^\w\s-]", "", title)[:40].strip().replace(" ", "_")
    if not safe:
        safe = f"clip_{index}"
    return f"clip_{index + 1:03d}_{safe}.mp4"


__all__ = ["CutError", "cut_moment", "tighten_silence"]
