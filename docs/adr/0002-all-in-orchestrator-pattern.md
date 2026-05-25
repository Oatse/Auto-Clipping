# ADR-0002: All In is an orchestrator over existing pipelines, not a unified service layer

**Status:** Accepted (2026-05-25)

**Context:**

The **All In** Workspace (Workspace · 04) chains the same work that
**Clip Finder**, **Auto-Subtitle**, and **Short Maker** already do:

1. Download YouTube source + transcript (Clip Finder)
2. Extract multimodal signals + run Gemini moment detection (Clip Finder)
3. Cut each Moment from the source (new — frame-accurate local FFmpeg)
4. Optional silence trim (new — `ffmpeg silencedetect`)
5. Reframe to target aspect ratio (Short Maker)
6. Optional auto-subtitle burn-in (Auto-Subtitle)
7. Stream finished Clips back as Clip Cards in the UI

Two implementation shapes were viable:

- **(A) Orchestrator over the existing public APIs** — `_run_all_in_job`
  calls `ClipFinder`, `run_render_pipeline`, and Short Maker's renderer
  in sequence. Each existing workspace stays untouched.
- **(B) Extract a shared service layer first** —
  `transcript_service`, `moment_service`, `reframe_service`,
  `caption_service` — and have all four workspaces (including the
  existing three) consume that layer.

**Decision:**

Ship **A**, with B-shaped folder layout and service contracts from day
one so the future B refactor is rename-and-relocate, not redesign:

```
web/services/all_in/
├── __init__.py
├── models.py           ← AllInJob, AllInClip, status enums
├── presets.py          ← Bold / Minimal / Karaoke caption presets
├── runner.py           ← run_all_in_job(job)  (orchestrator entry)
└── stages/
    ├── __init__.py
    ├── source.py       ← download full source + audio
    ├── moments.py      ← adapter to processors.clip_finder
    ├── cut.py          ← range cut + silence trim
    ├── reframe.py      ← compute crop + call short_maker
    └── caption.py      ← adapter to web.services.pipeline_runner
```

Each `stages/*.py` exposes one async function with a small,
context-free signature (input paths, output dir, options) and returns
a typed result. The runner composes them. When the B refactor lands,
each stage moves to `web/services/{stage}_service.py` and the existing
workspaces start importing from the new location — no signature
change.

**Surgical extractions required for v1 (not full B refactor):**

1. `processors/short_maker.compute_smart_static_crop(video_path, target_ratio) -> CropBox`
   — single MediaPipe pass on N=20 sample frames, median centroid,
   centre fallback. ~80 lines. New code, no existing caller affected.
2. `processors/short_maker.reframe_to_ratio(input, output, ratio, crop)`
   — function-callable equivalent of the Short Maker renderer.
   The existing Short Maker UI flow keeps using its current entry
   point (no behaviour change); All In calls the new one.
3. `web/services/all_in/stages/caption.py::run_auto_subtitle_for_clip` —
   thin adapter that builds a synthetic `Job` and reuses
   `pipeline_runner.run_render_pipeline`. ~20 lines. Avoids a full
   extraction of the auto-subtitle pipeline for v1.

**Consequences:**

- **Faster ship.** All In v1 lands in days, not weeks. The B refactor
  is unblocked but not on the critical path.
- **Two caption code paths until v1.1.** The existing `Job`-driven
  flow and All In's adapter both reach `run_render_pipeline`.
  Behaviour parity is maintained because the adapter is *thin* — the
  rendering logic still has one home.
- **Folder layout = future contract.** The `stages/*.py` boundaries
  are the same boundaries B will use. Each stage is independently
  testable and replaceable. Teaching tests against `stages/cut.py`
  today is teaching tests for the eventual `cut_service.py`.
- **Per-clip retry stays cheap** (see Q10/Q12 in design grilling) —
  source persists with the Job (`output/all_in/{job_id}/source.mp4`),
  and `runner.retry_clip(job, clip_idx)` re-enters the per-clip loop
  starting at `cut.py`, skipping `source.py` and `moments.py`.

**Rejected alternatives:**

- **(B) Full extraction first.** Right architecture, wrong moment.
  2–3 weeks of refactor before the first All In Job runs, with no
  user-visible benefit during that time. Adopted as the v1.1 target.
- **(C) Inline / copy-paste the chain.** Triple maintenance burden
  for Gemini prompts, ElevenLabs key rotation, and FFmpeg crop math.
  Rejected outright.
