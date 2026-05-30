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

**Cut Strategy**:
Refinement rule that derives a final **Moment** from a base time-range produced by Clip Finder. Three named strategies in v1: `tight` (head/tail trimmed to the punchline), `hooky` (start snapped to the first hook line within ±3 s), `context` (start padded back to the previous topic boundary, capped at +20 s). Multiple Cut Strategies on the same base time-range produce multiple **Moments** — each still a 1-to-1 with its eventual **Clip**.
_Avoid_: Variant, version, cut type.

**Scoring Profile**:
Named bundle of `ClipScore` weights tuned for a content style — currently `vtuber` (default), `podcast`, `news`, `gaming`, `asmr`. The profile only changes how candidate **Moments** are ranked; it never affects detection, boundary refinement, or rendering. Stored on the Job so re-runs are reproducible.
_Avoid_: Niche, genre, mode (mode is the detection mode: single-shot vs multi-stage).

**Hook**:
The first 1-3 seconds of a **Moment** that determine whether a viewer keeps watching. The Hook Optimizer is the boundary-refinement pass that snaps `Moment.start` to a strong opening line (question, exclamation, name-drop) when one exists in a ±3 s window.
_Avoid_: Intro, opener, lead.

**Punchline**:
The single word (or short phrase) inside a **Moment** that carries the payoff — the one the renderer should pop / colour / zoom. Tagged by Gemini during scoring as `punchline_offset` (seconds from `Moment.start`). Consumed by `cut_strategies.tight` to anchor the trim tail and by the captioning stage as a hint for emphasis.
_Avoid_: Highlight word, key word.

**Crowd Sync**:
A bonus dimension in `ClipScore` that fires when an audio peak and a chat spike fall inside the same time window — the moments where the creator and the audience reacted in unison, not just one of them. Stored as `score.coincidence_bonus` (0-10) on the **Clip**, and surfaced on the **Clip Card** as a flame marker once it crosses a salience threshold.
_Avoid_: Coincidence, hot moment, combo, resonance, spike sync.

**Scene Cut**:
A timed visual transition extracted from the source video using ffmpeg's `select=gt(scene,N)`. Stored as a `SignalEvent` of kind `SCENE_CUT`. Reframe re-runs its smart-static crop computation per scene segment so the subject stays in frame after a cut.
_Avoid_: Visual cut, edit point.

**Clip Sidecar**:
A `.metadata.json` file written next to each finished **Clip** containing Gemini-generated upload-ready fields: title, description, hashtag list, suggested thumbnail timestamp. Read by the API when the user asks "what should I caption this on TikTok / YT Shorts".
_Avoid_: Manifest, summary file, info.

**All In**:
The Workspace that chains Clip Finder's moment detection, Auto-Subtitle's captioning, and Short Maker's reframing into one job. Produces a list of finished, captioned, reframed **Clips** from a single YouTube URL.
_Avoid_: Auto Clip Maker, Auto Maker Clip, Full Auto, One-Shot.

**Style Preset**:
Named bundle of translation-tone rules consumed by the Auto-Subtitle translator. Two presets in v1: `natural` (default — conversational, idiom-functional, light fillers, no internet slang) and `formal` (full sentences, no contractions, neutral register). Affects only how the translator phrases the target text; never affects STT, Moment detection, segment timing, or rendering. Stored on the Job so re-runs are reproducible.
_Avoid_: Tone, voice, profile (profile is **Scoring Profile**), mode (mode is the detection mode).

**Style Note**:
Optional free-form user instruction appended additively to the active **Style Preset** for one **Job** (e.g. "keep JP honorifics like senpai/shishou", "academic register"). Lives in the Auto-Subtitle workspace's advanced section and on All In's caption settings. Never replaces the Style Preset — only extends it.
_Avoid_: Custom prompt, instruction, hint, override.

## Relationships

- A **Source video** yields zero or more **Moments**.
- A **Moment** plus refinement settings produces exactly one **Clip**.
- A **Clip** is rendered into exactly one **Clip Card** in the UI.
- An **All In** Job is the only Job kind that owns the full chain Source → Moment → Clip → Clip Card.

## Flagged ambiguities

- "Auto Clip Maker" / "Auto Maker Clip" was used during initial design — resolved: the Workspace is **All In**.
