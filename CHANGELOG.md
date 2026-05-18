# CHANGELOG

A focused record of the May 2026 audit + refactor + UI polish work.
For tagged-release-style summaries, see git log; for the architectural
rationale behind the timing changes, see `docs/adr/0001-timing-source-of-truth.md`.

---

## 2026-05-18 — Audit + Refactor + UI Polish (P0–P15)

### Backend (P0–P3)

**P0 — Timing accuracy fixes (`fix/p0`)**
- `processors.timing.Sanitizer` is now **speaker-aware**: cross-speaker
  overlap (interruption) preserved; only same-speaker overlap trimmed.
- Word-duration cap auto-loosens via a global speech-rate factor
  (median of `actual / estimate`, clamped 1.0–3.0).
- `elevenlabs_words_raw.json` saved before any sanitization runs.
- Timeline drag now linearly rescales `seg.words[]` so karaoke
  highlight stays anchored to the segment box.
- DeepL API key removed from source; reads from `config.DEEPL_API_KEY`.

**WhisperX removal**
- Deleted `processors/transcription.py`, `processors/double_check.py`,
  `models/whisper-anime/`.  ElevenLabs Scribe is the only STT engine.
- `torch` import made defensive — only used to surface GPU info.
- UI dropdown collapsed to a single-engine entry.

**P1 — Architecture refactor**
- `processors/timing/`  — `TimingPolicy` dataclass + `Sanitizer` class.
- `processors/stt/`     — `SttEngine` Protocol + `ElevenLabsSttEngine`.
- `processors/translator/` — split 1369-line file into 7 focused
  modules: `constants`, `gemini_client`, `regrouper`, `recheck`,
  `deepl`, `local_grouper`, `processor`.
- `web/services/`      — `job_models`, `transcript_sync`,
  `pipeline_runner`.  `web/server.py` shrank by 487 LOC.

**P2 — Test coverage**
- 51 new unit tests across `tests/test_timing.py`,
  `tests/test_recheck.py`, `tests/test_translator.py`,
  `tests/test_transcript_sync.py`.  Total: 117 passing.

**P3 — Docs**
- README rewritten for the ElevenLabs-only architecture.
- `docs/adr/0001-timing-source-of-truth.md` records the decision
  that ElevenLabs words are the canonical timing source.

### Frontend / UI (P4–P15)

**P4 — Polish layer (`web/static/css/polish.css`)**
- New CSS file imported last; ~335 LOC of motion tokens, custom
  easing curves (`ease-out`, `ease-in-out`), standard durations
  (`--dur-press`, `--dur-fast`, `--dur-base`, `--dur-slow`).
- `transform: scale(0.97)` press feedback on every primary control,
  gated behind `@media (hover: hover) and (pointer: fine)`.
- `:focus-visible` 2 px accent ring on all interactive elements.
- `prefers-reduced-motion` safety net.
- Yellow contrast: new `--warn-on-bg` (amber-200) for text-on-dark.
- `.cf-clip-card` locked to `max-height: 520 px` with internal scroll;
  score chip promoted to a gradient pill.
- CF instructions textarea + render-options modal polished.

**P5 — Backend signals reflected in UI**
- Preview transcript-toggle relabelled `Raw ElevenLabs` ↔ `Refined`.
- System Info card now surfaces Gemini + DeepL availability.
- Clip Finder sorts clips by score (descending), original index
  preserved for download URLs.

**P6 — Polish extension**
- Replaced remaining `transition: all` declarations with explicit
  property lists across `.job-card`, `.btn-resume-job`,
  `.btn-view-job`, `.cf-clip-play-btn`, `.cf-clip-download`, form
  inputs, style-panel chips, nav tabs, drop-zone, wave-bar.
- Running jobs get a 1.6s pulsing accent stripe + soft inner glow.
- Progress bars get a 1.8s diagonal sheen overlay.
- More `:active` scale targets covering small buttons.
- Empty-state standardised.

**P7 — Terminology + Recommended badge**
- Render-options modal aligned with Preview toggle wording.
- `.render-option-badge` 'Recommended' pill on the Refined option.
- New `.cf-results-bar` strip showing `N clips · ranked by score`.
- Stagger animation switched from `:nth-child` to `var(--cf-card-i)`
  CSS variable.

**P8 — Toast notifications**
- `utils.js` shared `toast()` API with `info / success / warn /
  error` variants; container created lazily; aria-live=polite.
- Hover pauses the auto-dismiss timer; click-to-dismiss button.
- All 19 native `alert()` call sites replaced.

**P9 — In-app dialogs**
- `confirmDialog()` + `promptDialog()` promise-based replacements
  for `window.confirm` / `window.prompt`.
- Esc + overlay click cancel.  Focus trap + ARIA dialog/modal.
- 5 native confirm/prompt sites replaced (effects, jobs ×2,
  timeline, popups).

**P10 — Global normalisation**
- One global scrollbar rule (Firefox + Chromium), 8 px gutter, accent
  thumb with hover state.
- All text-style inputs share the same surface, hover/focus border
  colour, and 3 px accent halo.
- Range sliders + checkboxes/radios styled to match the theme.

**P11 — Skeletons + empty states**
- `.skeleton` primitive with 1.6 s shimmer (reduced-motion safe).
- `.skeleton-job-card` and `.skeleton-clip-card` mirror the real
  layouts for zero-shift transitions.
- Empty states upgraded to `.empty-title` + `.empty-sub` pattern.

**P12 — Success/error toasts**
- Manual transcript save fires `toast.success('Transcript saved')`
  (auto-save stays silent — the chip indicator covers it).
- Render completion / failure now also fires success/error toasts.

**P13 — Empty-state CTA + save spinner**
- 'Jump to upload' CTA on empty jobs panel; smooth-scrolls + bumps
  the drop-zone briefly.
- `.btn-save.is-saving` shows an inline spinner during save.

**P14 — Render phase items**
- Done phases get a green ✓ checkmark.
- Active phase pulses (1.8 s ring + 1 px translateY).
- Transcribing bar gradient now shifts continuously.

**P15 — Drop-zone polish**
- Explicit transition list, kinetic drag-over state (subtle scale +
  ring shadow + icon bump), `:focus-visible` ring for keyboard users.
