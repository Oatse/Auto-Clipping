# ADR-0001: ElevenLabs words are the canonical timing source

**Status:** Accepted (2026-05-18)

**Context:**

Before the May 2026 audit / refactor, the project mutated word- and
segment-level timestamps in **four** independent locations:

1. `models.transcript.sanitize_timestamps` — 4 passes including a
   non-speaker-aware word-overlap trim.
2. `processors.translator.recheck_word_level_alignment` — 9 passes that
   re-snapped boundaries to ElevenLabs source words.
3. `web.server._sync_segment_words_with_text` — proportional
   redistribution of word timestamps when the user edited segment text.
4. `processors.subtitle_renderer._build_ass_content` — same-speaker
   overlap fix duplicated *again* during ASS generation.

Each mutator had its own assumptions about the input.  The combination
produced visible timing drift between the on-screen subtitles and the
audio: cross-speaker interruptions were over-trimmed, fast/elongated
speech was clipped, drag-edits in the preview did not update word
timestamps, and the "ElevenLabs Original" toggle in the preview UI
displayed already-sanitized data.

**Decision:**

* The **ElevenLabs Scribe response** (`raw_result["words"]`) is the
  single canonical source of truth for word timing.
* The STT engine writes that data to disk as
  `phase1_transcription/elevenlabs_words_raw.json` *before* any
  sanitization runs.  The Preview UI toggle "ElevenLabs Original" reads
  from this file.
* All timing fixes go through one seam:
  `processors.timing.Sanitizer(TimingPolicy()).sanitize(...)`.
  Legacy callers (`models.transcript.sanitize_timestamps`) are kept as
  thin shims delegating to the same class.
* The sanitizer is **speaker-aware**: cross-speaker overlap (one
  speaker interrupting another) is preserved as a natural
  conversation; only same-speaker overlap is trimmed because no single
  speaker can physically produce two segments at once.
* The word-duration cap auto-loosens via a global speech-rate factor
  (median of `actual_duration / character_estimate`, clamped 1.0–3.0)
  so fast or emotionally-elongated speech is not clipped.
* The word-level recheck (`processors.translator.recheck`) is the
  authoritative reconciliation pass against the ElevenLabs source.
  When the user supplies an edited transcript via
  `style_config["transcript"]`, the recheck is **skipped** and only
  segment-level passes run — because
  `web.services.transcript_sync.sync_segment_words_with_text` has
  already redistributed word timestamps proportionally to match the
  user's edit, and those new timestamps will not match the ElevenLabs
  source by `(start, end)` key.

**Consequences:**

* **Single source of truth** — there is one place to look when a timing
  bug appears.  All four legacy mutators now route through
  `processors.timing.Sanitizer`.
* **Preview vs render parity** — what the user sees in the preview
  matches what FFmpeg burns into the video.  When the user drags a
  segment edge, `web/static/js/timeline.js` linearly rescales
  `seg.words[]` so karaoke / narration-pop highlight stays anchored to
  the segment box.
* **Tunability** — every threshold (silence_cap, char_to_seconds,
  elongation_per_char, same-speaker gap) lives in
  `processors.timing.policy.TimingPolicy`, a frozen dataclass passed
  by reference.  Tests exercise non-default policies.
* **Compat shim cost** — `models.transcript.sanitize_timestamps`
  continues to exist as a thin wrapper.  Lazy imports inside the
  function body avoid a circular `models ↔ processors` dependency.

**Test coverage:**

`tests/test_timing.py` (21), `tests/test_recheck.py` (10),
`tests/test_translator.py` (16), `tests/test_transcript_sync.py` (4).

**Status of legacy paths:**

* `processors.translator.TranslatorProcessor.recheck_word_level_alignment`
  remains as a static-method alias to the function in
  `processors.translator.recheck`.  `web/server.py` imports through the
  alias, so external callers don't break.
* `processors.elevenlabs_stt.ElevenLabsSTTProcessor` is an alias for
  `processors.stt.ElevenLabsSttEngine`.  Re-exported from
  `processors/__init__.py` so `from processors import ...` still works.
