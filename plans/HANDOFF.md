# Handoff — Auto-Clip state as of 2026-08-31

Written for a fresh session. Everything below is on branch
**`feat/compilation-premiere-handoff`** (35 commits ahead of `main`, pushed).
**`main` is untouched** — the original app still runs from it.

**607 tests green.** Run: `python -m pytest -q -p no:logging`

---

## 1. What this branch added

Two features, plus fixes to things that turned out to be broken already.

### Judgment refocus (VTuber-only)
Branch `refactor/vtuber-only-clip-judgment`, folded into this one.
Collapsed five scoring niches to one VTuber profile and added three
research-backed dimensions: `interaction_dynamic` (collab chemistry — the
largest clip category), `en_translatability`, `format_fit`. Detection prompt
rewritten around chat as primary evidence. Plan: `plans/vtuber-only-refocus/`.

### Compilation → Premiere handoff
One URL becomes a Premiere timeline of its best moments.

```
URL ─┬─ download master VOD ──────────────┐
     └─ subs + chat + audio → judgment ───┤   (these two run in parallel)
                                          ▼
              moments above a quality bar (6.0), chronological
                                          ▼
              cut each moment to its own file (handles ±15s)
                                          ▼
              FCPXML → Premiere timeline
                                          ▼
      [you edit]  →  "Subtitle open timeline"  →  captions back on tracks
```

Plan: `plans/compilation-premiere-handoff/plan.md` (all decisions D1–D11).

---

## 2. Where the code lives

### `processors/premiere/` — the Premiere-facing half (new)
| file | lines | role |
|---|---|---|
| `subtitle_loop.py` | 611 | export timeline audio → transcribe → translate → SRT → back into Premiere |
| `bridge_client.py` | 294 | Python → Premiere over the connector's file-IPC |
| `fcpxml.py` | 289 | FCP7 XML timeline generation |
| `pipeline.py` | 282 | one URL → master + timeline, analysis and download in parallel |
| `graphics.py` | 260 | captions as Essential Graphics clips (**limited — see §4**) |
| `source.py` | 248 | master/audio download + ffprobe metadata |
| `clip_extract.py` | 170 | cut moments out of the master for editable playback |

### `cep-panel/` — the Premiere panel (new)
`main.js` (529 lines) is the whole UI: posts jobs to the local server, polls
them, drives Premiere via `host.jsx`. Installed by `launcher/panel_install.py`,
which **signs it** (see §4).

### `launcher/` — process orchestration (new)
`python -m launcher` brings up app + Premiere + bridge, idempotently. Also
`--install-panel` / `--status`.

### `web/` — the existing app (mostly untouched)
Routers mounted at boot: `system, pages, short_maker, auto_subtitle,
clip_finder, all_in, multi_pov, compilation`.
Only `compilation.py` is new; the rest predate this work.

---

## 3. Fixes to pre-existing bugs (not caused by this work)

* **The app could not boot at all.** Starlette 1.0 removed
  `add_event_handler`, which all four workspace restore hooks used, so
  `import web.server` raised. Fixed with `web/services/startup.py`
  (`register_startup` composes the lifespan). Tests never caught it because
  route tests build their own FastAPI app.
* **Segment subtitle effects vanished on reload.** The transcript GET rebuilt
  segments without `effect` / position, though the PUT saved them.
* Judgment bugs: punchline lost across boundary refinement, silent scorer
  fallback, top clips tying at the 10.0 cap.

---

## 4. Hard-won gotchas — read before touching Premiere code

These cost real time and two Premiere crashes.

1. **The panel must be signed.** `PlayerDebugMode` alone is no longer enough:
   an unsigned extension is refused with `Signature verification failed` in
   `%TEMP%\CEP11-PPRO.log` and **simply never appears, with no UI error**. The
   installer self-signs with Adobe's ZXPSignCmd (gitignored, fetch per
   `cep-panel/README.md`).

2. **Never put `--` inside an XML comment** in `CSXS/manifest.xml`. Illegal
   XML; CEP rejects the whole extension silently. The installer now validates
   the manifest before copying.

3. **Paths handed to Premiere must be absolute.** It runs with its own working
   directory, so a relative path resolves to nothing and surfaces as a
   confusing "file not found".

4. **`app.openFCPXML` takes two arguments** (xml, destinationProject) and
   creates a *separate* project. To import into the open one, use
   `project.importFiles`.

5. **`importMGT` does not scale — it crashed Premiere twice.** Each call
   unpacks an ~800 KB template and adds a project item; 276 captions took the
   app down even split into 14 batches, because the cost is per import, not
   per call. `graphics.py` is therefore capped at 40 and is not the default.
   **For a whole timeline the answer is Premiere's own bulk operation**:
   import a caption track, then `Captions > Upgrade Caption to Graphic`.

6. **`createCaptionTrack` exists and works** — my earlier "no caption API"
   claim was wrong, because `for...in` does not enumerate native methods. One
   call places a whole SRT on a caption track regardless of cue count, which
   is why it scales where `importMGT` does not. Calling it once per file gives
   **separate caption tracks that display simultaneously** — how overlapping
   speech is handled now.

7. **No caption readback on this host.** `getCaptionTracks` and QE's
   `numCaptionTracks` are both `undefined`, so the code reports what Premiere
   *accepted*, never what it can prove appeared. Verification is visual.

8. **Detached child processes need `stdio=DEVNULL`.** Without it the launcher's
   spawned server died on its first `print` and looked like "never started".

---

## 5. Verified against the real app

* Clip judgment on a 113-minute Hololive VOD: 12 moments, 18.6 min, threshold
  6.0 confirmed. `format_fit` went from 5.0 to 10.0 per moment after the
  compilation-mode fix.
* Timeline lag diagnosed by measurement: the YouTube master is long-GOP with a
  keyframe every ~6 s, so at 60 fps Premiere decoded ~360 frames per seek
  inside a 2.5 GB file. Cutting moments to their own files with a 1-second GOP
  fixed it (NVENC, 30 s for a 190 s moment).
* Subtitle loop: `exportAsMediaDirect` rendered a 13.4-minute timeline to WAV
  in 15 s; a real pass produced 247 valid UTF-8 cues.
* Caption tracks: two overlapping test cues landed on two tracks and displayed
  together. Test items were cleaned out afterwards.

---

## 6. Next session: making it lighter

The stated goal is **separating the extension's logic from the web version so
each is lighter**. Starting observations, not conclusions:

* **The web server boots everything.** `web/server.py` mounts all eight
  routers, and each restore hook scans disk at startup (last run restored 8 +
  58 + 3 jobs). The panel only ever calls `/api/compilation/*`, so a panel-only
  process could mount one router and skip the rest.
* **`processors/premiere/` is already standalone.** It imports from
  `models/`, `processors/clip_finder/`, and `processors/stt|translator`. It does
  not depend on `web/` at all — so a slim service is mostly a wiring question,
  not a rewrite.
* **The heavy import chain is `processors/clip_finder/__init__.py`**, which
  pulls `orchestrator` → `audio_signals` → `yt_dlp` at import time. Anything
  that touches clip_finder pays for yt-dlp even when only subtitling.
* **`subtitle_loop.py` at 611 lines is the biggest module** and mixes four
  concerns: audio export, transcription orchestration, SRT authoring, and
  Premiere placement. SRT authoring (`build_srt`, `resolve_overlaps`,
  `speaker_label`, `write_per_speaker_srt`) is pure and has no Premiere
  dependency — a natural split.
* **`cep-panel/main.js` at 529 lines** holds compilation UI and subtitle UI in
  one file; they share only the connection helpers.

Things to keep in mind while splitting:

* The panel is **signed at install time**, so any file added to `cep-panel/`
  must go through `python -m launcher --install-panel`, not be edited in the
  extensions folder.
* `panel-config.json` is written **at install time** and is how the panel finds
  the project root and Python — a split must keep that contract.
* The auto-subtitle text pipeline (`translate(regroup=True)` then
  `apply_natural_caption_style`) is the tuned one; the subtitle loop
  deliberately mirrors `web/services/pipeline_runner.py`. Do not let a refactor
  drift them apart.

---

## 7. Environment facts

* Premiere Pro **2023 (23.2.0)** — CEP 11, no UXP. No After Effects installed.
* `premiere-pro-mcp` 1.14.4 installed globally; its connector panel must be
  open and **Started** (`Window > Extensions > MCP for Adobe Premiere Pro`).
* Bridge protocol: `{tmp}/premiere-mcp-bridge/` — `bridge-heartbeat.json`
  (1 s cadence, stale = dead), `cmd_{id}.jsx` in, `res_{id}.json` out.
* App server on **:7860** (`run_web.py`), 9router proxy on **:20128**.
* node v22.14 (below premiere-pro-mcp's stated `>=22.22`, works so far),
  ffmpeg + NVENC available (RTX 3050).
* Gemini keys ×4; models selectable: Gemini, `cc/claude-opus-4-6` (9router),
  Codex `cx/*`.
