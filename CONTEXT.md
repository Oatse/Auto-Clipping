# ClipAuto

ClipAuto turns long-form source videos (livestreams, VODs, uploads) into short, finished, captioned clips through a stack of opinionated pipelines. Each "workspace" page in the web UI is one pipeline shape with its own job model.

## Language

**Workspace**:
A single page in the web UI that owns one pipeline shape end-to-end.
Existing: **Auto-Subtitle** (01), **Clip Finder** (02), **Short Maker** (03), **All In** (04).
_Avoid_: Page, tool, module.

**Job**:
One run of one workspace's pipeline against one source video. Each workspace has its own job model (`Job`, `ClipFinderJob`, `AllInJob`).
_Avoid_: Task, request, render.

**Source video**:
The full input the user pointed us at — a YouTube URL or an uploaded file. Always one item per Job.
_Avoid_: Input, original, master.

**Moment**:
A scored time range `(start, end, title, reason, score)` in the source video that Gemini judged worth clipping. Output of the **Clip Finder** analysis stage; consumed by anything that cuts video.
_Avoid_: Highlight, segment, hit.

**Clip**:
A finished video file produced from a **Moment**. Always rendered, never just a time range.
_Avoid_: Cut, segment, output.

**Clip Card**:
The UI surface for a finished **Clip**: thumbnail, title, brief description, rating, download button.
_Avoid_: Result, item, tile.

**All In**:
The Workspace that chains Clip Finder's moment detection, Auto-Subtitle's captioning, and Short Maker's reframing into one job. Produces a list of finished, captioned, reframed **Clips** from a single YouTube URL.
_Avoid_: Auto Clip Maker, Auto Maker Clip, Full Auto, One-Shot.

## Relationships

- A **Source video** yields zero or more **Moments**.
- A **Moment** plus refinement settings produces exactly one **Clip**.
- A **Clip** is rendered into exactly one **Clip Card** in the UI.
- An **All In** Job is the only Job kind that owns the full chain Source → Moment → Clip → Clip Card.

## Flagged ambiguities

- "Auto Clip Maker" / "Auto Maker Clip" was used during initial design — resolved: the Workspace is **All In**.
