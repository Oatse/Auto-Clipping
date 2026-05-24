"""
web.services.pipeline_runner — Run the 4-phase auto-subtitle pipeline.

Two top-level coroutines:

* :func:`run_transcription_only` — Phase 1 only (ElevenLabs STT +
  optional Gemini auto-translate).  Used when the user wants to review
  the transcript before rendering.
* :func:`run_render_pipeline`    — Phase 2-4.  Picks up either the
  user-edited transcript supplied via ``style_config["transcript"]`` or
  the cached Phase 1 transcript on disk.

Both coroutines mutate the supplied :class:`~web.services.job_models.Job`
in place (status, progress, log_lines).  They never raise — failures are
recorded as ``JobStatus.FAILED`` on the job.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from loguru import logger

import config
from models.transcript import (
    TranscriptSegment,
    WordTimestamp,
    sanitize_timestamps,
)

from .job_models import Job, JobStatus, PHASE_LABELS
from .transcript_sync import sync_segment_words_with_text


def _make_logger(job: Job):
    """Build a closure that writes to both ``job.log_lines`` and loguru."""
    def log(msg: str) -> None:
        job.log_lines.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        logger.info("[Job {}] {}", job.id[:8], msg)
    return log


def _make_phase_setter(job: Job, log_fn):
    """Build a closure that updates ``current_phase`` / ``progress_pct``."""
    def set_phase(phase: int) -> None:
        job.current_phase = phase
        job.phase_label = PHASE_LABELS.get(phase, f"Phase {phase}")
        job.progress_pct = round((phase - 1) / 4 * 100, 1)
        log_fn(f"▶ Phase {phase}/4: {job.phase_label}")
    return set_phase


def _persist_job_meta(job: Job, output_dir: Path) -> None:
    """Write job_meta.json so the FastAPI restore handler can rehydrate
    this job after a server restart.

    Persisting after every terminal status change (not just transcription)
    keeps ``target_language`` and ``transcribe_only`` accurate so the
    Recent Jobs list shows the correct language instead of the literal
    fallback "en" the restore handler used to assume.
    """
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        meta_path = output_dir / "job_meta.json"
        with meta_path.open("w", encoding="utf-8") as f:
            json.dump(
                job.model_dump(exclude={"log_lines"}),
                f, ensure_ascii=False, indent=2,
            )
    except Exception as exc:  # noqa: BLE001 — meta is best-effort
        logger.warning(
            "[Job {}] Could not save job_meta.json: {}", job.id[:8], exc,
        )


# ─── Phase 1 only ─────────────────────────────────────────────────────────

async def run_transcription_only(
    job: Job,
    video_path: Path,
    target_language: str,
) -> None:
    """
    Phase 1 only — ElevenLabs Speech-to-Text (+ optional Gemini auto-translate).

    Pipeline stops after Phase 1 so the user can review the transcript in
    the Preview screen before triggering the render.
    """
    job.status = JobStatus.RUNNING
    job.started_at = time.time()

    output_dir = Path("./output") / job.id

    log = _make_logger(job)
    set_phase = _make_phase_setter(job, log)

    try:
        log(f"Memulai transkripsi: {video_path.name}")
        if job.num_speakers:
            log(f"Max speakers hint: {job.num_speakers} (actual may be lower)")
        if not job.speaker_detection:
            log("Speaker detection: OFF (single-speaker mode)")

        log("Model: ElevenLabs Speech-to-Text")

        set_phase(1)

        from processors.stt import ElevenLabsSttEngine

        if not config.ELEVENLABS_API_KEYS:
            raise ValueError("ELEVENLABS_API_KEY is not set in .env")

        log("Using ElevenLabs Speech-to-Text API...")
        engine = ElevenLabsSttEngine()
        segments, _ = await engine.transcribe(
            video_path=video_path,
            output_dir=output_dir / "phase1_transcription",
            speaker_detection=job.speaker_detection,
            num_speakers=job.num_speakers,
        )
        log(f"✓ ElevenLabs transkripsi selesai: {len(segments)} segmen (bahasa asal)")

        # Save the post-sanitize / pre-translate snapshot used by the
        # legacy "ElevenLabs Original" toggle.  The fully-raw snapshot is
        # written by the engine itself (elevenlabs_words_raw.json).
        el_original_dir = output_dir / "phase1_transcription"
        el_original_dir.mkdir(parents=True, exist_ok=True)
        el_original_path = el_original_dir / "elevenlabs_original_transcript.json"
        el_original_data = []
        for seg in segments:
            seg_d = {
                "start": seg.start,
                "end": seg.end,
                "text": seg.text,
                "speaker": getattr(seg, "speaker", "SPEAKER_00"),
            }
            if hasattr(seg, "words") and seg.words:
                seg_d["words"] = [
                    {
                        "word": getattr(w, "word", ""),
                        "start": getattr(w, "start", 0),
                        "end": getattr(w, "end", 0),
                    }
                    for w in seg.words
                ]
            el_original_data.append(seg_d)
        with el_original_path.open("w", encoding="utf-8") as f:
            json.dump({"segments": el_original_data}, f, ensure_ascii=False, indent=2)
        log(f"✓ ElevenLabs original transcript saved: {el_original_path.name}")

        # Auto-translate via Gemini if a target language is set.
        if target_language and config.GEMINI_API_KEYS:
            log(f"Auto-translating to '{target_language}' via Gemini...")
            if not getattr(config, "DEEPL_API_KEY", ""):
                log(
                    "Note: DEEPL_API_KEY not set. If Gemini fails, subtitles "
                    "will be left in the SOURCE language (no fallback)."
                )
            from processors.translator import TranslatorProcessor
            translator = TranslatorProcessor(target_language=target_language)
            segments, _ = await translator.translate(
                segments=segments,
                output_dir=output_dir / "phase2_translation",
                regroup=True,
            )
            log(
                f"✓ Auto-translate + word-level recheck selesai: "
                f"{len(segments)} segmen → '{target_language}'"
            )
        elif not config.GEMINI_API_KEYS:
            log("⚠ No GEMINI_API_KEYS configured — skipping auto-translate")

        # Persist canonical transcript JSON.
        transcript_output_dir = output_dir / "phase1_transcription"
        transcript_output_dir.mkdir(parents=True, exist_ok=True)
        transcript_file = transcript_output_dir / "source_transcript.json"

        segments_data = []
        for seg in segments:
            seg_dict = {
                "start": seg.start,
                "end": seg.end,
                "text": seg.text,
                "speaker": getattr(seg, "speaker", "SPEAKER_00"),
            }
            if hasattr(seg, "words") and seg.words:
                seg_dict["words"] = [
                    {
                        "word": getattr(w, "word", getattr(w, "text", str(w))),
                        "start": getattr(w, "start", 0),
                        "end": getattr(w, "end", 0),
                    }
                    for w in seg.words
                ]
            segments_data.append(seg_dict)

        with transcript_file.open("w", encoding="utf-8") as f:
            json.dump({"segments": segments_data}, f, ensure_ascii=False, indent=2)

        job.transcript_path = str(transcript_file)
        job.status = JobStatus.COMPLETED
        job.current_phase = 1
        job.progress_pct = 25.0
        job.phase_label = "Transcription complete — Ready for preview"
        job.completed_at = time.time()
        elapsed = round((job.completed_at or 0) - (job.started_at or 0), 1)
        log(f"✓ Transkripsi selesai dalam {elapsed}s → Siap untuk preview")

        # Persist metadata so jobs survive a server restart.
        _persist_job_meta(job, output_dir)

    except asyncio.CancelledError:
        job.status = JobStatus.CANCELLED
        job.phase_label = "Cancelled"
        log("Job dibatalkan")

    except Exception as exc:  # noqa: BLE001
        job.status = JobStatus.FAILED
        job.phase_label = "Failed"
        job.error = str(exc)
        log(f"✗ Error: {exc}")
        logger.exception("[Job {}] Transcription failed", job.id[:8])


# ─── Phase 2-4 ────────────────────────────────────────────────────────────

async def run_render_pipeline(
    job: Job,
    video_path: Path,
    target_language: str,
    style_config: dict,
) -> None:
    """
    Run Phase 2-4 using the existing transcript.

    Called after the user finishes adjusting subtitle style in the
    Preview screen.  When ``style_config["transcript"]`` is provided we
    skip Phase 2 (translation) so the user's edits aren't overwritten.
    """
    job.status = JobStatus.RUNNING
    job.started_at = time.time()

    output_dir = Path("./output") / job.id

    log = _make_logger(job)
    set_phase = _make_phase_setter(job, log)

    try:
        from main import VideoSubtitlePipeline

        log(f"Memulai render pipeline: {video_path.name}")
        if style_config:
            log(
                "Style: font={}, anim={}".format(
                    style_config.get("fontFamily", "default"),
                    style_config.get("animStyle", "word-pop"),
                )
            )
            fx_list = style_config.get("effects", [])
            if fx_list:
                log(f"Effects: {len(fx_list)} effect(s) on timeline")
            flt = style_config.get("filter", {})
            if flt and flt.get("name", "none") != "none":
                log(f"Color filter: {flt.get('name')}")

        pipeline = VideoSubtitlePipeline(
            input_video=video_path,
            output_dir=output_dir,
            target_language=target_language,
        )

        # ── Pick the transcript source ────────────────────────────────────
        user_transcript = (
            style_config.get("transcript") if style_config else None
        )
        transcript_source = (
            style_config.get("transcriptSource", "refined")
            if style_config else "refined"
        )

        if user_transcript and isinstance(user_transcript, list) and len(user_transcript) > 0:
            source_label = (
                "original ElevenLabs"
                if transcript_source == "original"
                else "refined (user-edited)"
            )
            log(f"✓ Menggunakan transkrip {source_label} dari preview")
            job.current_phase = 1
            job.phase_label = f"Transcript ({source_label})"
            job.progress_pct = 25.0

            segments: list[TranscriptSegment] = []
            for seg_dict in user_transcript:
                if isinstance(seg_dict, dict):
                    segments.append(TranscriptSegment.from_dict(seg_dict))

            # Sync word-level text with segment text.  Without this the
            # Pycaps word-pop renderer would show the pre-edit text.
            for seg in segments:
                sync_segment_words_with_text(seg)

            log(f"✓ Dimuat {len(segments)} segmen dari preview")

            # Skip Phase 2 — re-translating would overwrite the user's edits.
            set_phase(2)
            translated_segments = segments
            log("✓ Terjemahan di-skip (menggunakan teks dari preview)")
        else:
            # Fallback: load from Phase-1 cache and translate.
            transcript_file = output_dir / "phase1_transcription" / "source_transcript.json"
            if transcript_file.exists():
                log("✓ Menggunakan cache Phase 1 (skip re-transcribe)")
                job.current_phase = 1
                job.phase_label = "Transcription (cached)"
                job.progress_pct = 25.0

                with transcript_file.open("r", encoding="utf-8") as f:
                    raw = json.load(f)

                segments = [
                    TranscriptSegment.from_dict(seg)
                    for seg in raw.get("segments", [])
                ]
                log(f"✓ Dimuat {len(segments)} segmen dari cache")
            else:
                log("Tidak ada cache Phase 1, menjalankan transkripsi...")
                set_phase(1)
                segments, _ = await pipeline.transcriber.transcribe(
                    video_path=video_path,
                    output_dir=output_dir / "phase1_transcription",
                    num_speakers=job.num_speakers,
                    speaker_detection=job.speaker_detection,
                )
                log(f"✓ Phase 1 selesai: {len(segments)} segmen")

            # Phase 2 — Translation
            set_phase(2)
            translated_segments, _ = await pipeline.translator.translate(
                segments=segments,
                output_dir=output_dir / "phase2_translation",
            )
            log(
                f"✓ Terjemahan: {len(translated_segments)} segmen ke "
                f"'{target_language}'"
            )

        # ── Recheck word-level alignment before rendering ────────────────
        # Translator.translate() already runs recheck_word_level_alignment
        # internally against the words present at translate-time, so a
        # second pass here used to double the work for every render job.
        # We only need a fresh recheck when the user supplied an edited
        # transcript that was NOT translated this run — and even then
        # the proportional word redistribution done by
        # ``sync_segment_words_with_text`` produces timestamps that no
        # longer match the ElevenLabs source, so a recheck would only
        # introduce drift.  Hence: never recheck here.
        skip_recheck = bool(
            user_transcript
            and isinstance(user_transcript, list)
            and len(user_transcript) > 0
        )

        # Sanitize timing.  Word-level passes are skipped when the user
        # supplied a transcript — see ``sync_segment_words_with_text``.
        translated_segments = sanitize_timestamps(
            translated_segments,
            segment_level_only=skip_recheck,
        )

        # Phase 3 — Subtitle Rendering
        set_phase(3)
        pycaps_json = pipeline.subtitle_renderer.build_pycaps_transcript(
            segments=translated_segments,
            output_dir=output_dir / "phase3_subtitles",
        )
        subtitled_video = await asyncio.to_thread(
            pipeline.subtitle_renderer.render,
            video_path=video_path,
            pycaps_transcript=pycaps_json,
            output_path=output_dir / "phase3_subtitles" / "subtitled.mp4",
            style_config=style_config,
            segments=translated_segments,
            speaker_detection=job.speaker_detection,
        )
        log("✓ Subtitle rendering selesai")

        # Phase 4 — Final Mux
        set_phase(4)
        stem = video_path.stem
        final_output = await pipeline.muxer.mux(
            video_path=subtitled_video,
            output_path=output_dir / f"{stem}_subtitled_{target_language}.mp4",
        )

        job.status = JobStatus.COMPLETED
        job.progress_pct = 100.0
        job.phase_label = "Completed"
        job.output_file = str(final_output)
        job.completed_at = time.time()
        elapsed = round((job.completed_at or 0) - (job.started_at or 0), 1)
        log(f"✓ Render selesai dalam {elapsed}s → {final_output.name}")

        # Persist final state so the Recent Jobs list survives a restart
        # with target_language / output_file / status all preserved.
        _persist_job_meta(job, output_dir)

    except asyncio.CancelledError:
        job.status = JobStatus.CANCELLED
        job.phase_label = "Cancelled"
        log("Render dibatalkan")
        _persist_job_meta(job, output_dir)

    except Exception as exc:  # noqa: BLE001
        job.status = JobStatus.FAILED
        job.phase_label = "Failed"
        job.error = str(exc)
        log(f"✗ Error: {exc}")
        logger.exception("[Job {}] Render pipeline failed", job.id[:8])
        _persist_job_meta(job, output_dir)


async def _maybe_recheck(
    *,
    output_dir: Path,
    segments: list[TranscriptSegment],
    log,
    skip: bool,
) -> list[TranscriptSegment]:
    """Run the word-level recheck against the saved ElevenLabs source.

    Returns the rechecked segments, or the input unchanged when the
    recheck source file is missing or ``skip`` is True.
    """
    if skip:
        log("✓ Word-level recheck di-skip (user-edited transcript sudah di-sync)")
        return segments

    el_original_path = (
        output_dir / "phase1_transcription" / "elevenlabs_original_transcript.json"
    )
    if not el_original_path.exists():
        return segments

    try:
        from processors.translator import TranslatorProcessor as _TP

        with el_original_path.open("r", encoding="utf-8") as f:
            el_data = json.load(f)

        el_words: list[WordTimestamp] = []
        el_speakers: list[str] = []
        for seg_d in el_data.get("segments", []):
            sp = seg_d.get("speaker", "SPEAKER_00")
            for wd in seg_d.get("words", []):
                el_words.append(
                    WordTimestamp(
                        word=wd.get("word", ""),
                        start=wd.get("start", 0),
                        end=wd.get("end", 0),
                    )
                )
                el_speakers.append(sp)

        if el_words and any(s.words for s in segments):
            log(
                f"Running word-level recheck: {len(segments)} segments vs "
                f"{len(el_words)} ElevenLabs words"
            )
            segments = _TP.recheck_word_level_alignment(
                segments, el_words, el_speakers,
            )
            log("✓ Word-level recheck selesai")
    except Exception as exc:  # noqa: BLE001
        log(f"⚠ Word-level recheck skipped: {exc}")

    return segments
