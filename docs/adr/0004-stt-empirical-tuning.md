# ADR-0004: STT Sprint 1 — Empirical Tuning of ElevenLabs Scribe Calls

**Status:** Accepted (2026-05-28)

## Context

The May 2026 transcript audit raised a hypothesis: a large amount of
the gap between our subtitle output and ElevenLabs Studio output came
from API parameters we never sent. The audit also proposed dropping
~120 LOC by switching from our hand-written `_parse_response` parser to
the server-side `additional_formats=segmented_json` payload.

Before changing production code we ran a focused A/B test against two
real Job sources:

* `output/audit_ad4b2c61423d/` — single-speaker Japanese (MIKOvsGUNDAM,
  285 s). Confirms behaviour on CJK content.
* `output/audit_okayu/` — two-speaker Japanese (Okayu, 117 s).
  Confirms speaker-break handling.
* `output/audit_okayu_noverbatim/` — same audio with `no_verbatim=true`.
  Confirms (or refutes) the disfluency-removal pitch.

Findings, with line-count numbers from the recomputed `summary.txt`:

| Metric                        | Our parser | `segmented_json` |
|-------------------------------|-----------:|-----------------:|
| Segments (MIKO)               |         89 |               62 |
| Segments (Okayu)              |         63 |               44 |
| Avg words/seg (MIKO)          |       5.91 |             9.84 |
| Avg words/seg (Okayu)         |       9.21 |            14.41 |
| Speaker changes (Okayu)       |         27 |               27 |
| `multi_speaker_segments`      |          0 |                0 |
| `punctuation_only_segments`   |          0 |                2 |

The two parsers agree on **speaker-break placement** (27 = 27). They
disagree on **sentence-split aggressiveness**: we cut ~1.4× more often.
For Shorts/TikTok captions — ClipAuto's primary output — shorter
segments are preferable, so the over-split is a feature, not a defect.

`no_verbatim` was effectively a no-op on Japanese (text grew by 18
characters; word count fell by 1). The docs frame it as a generic
filler-removal toggle but our sample suggests it is English-first
today.

`logprob` is present in every Scribe v2 word entry (range
`[-inf, 0]`, `0` = certain). Sample values from MIKO: `-0.038`
(prob 0.96), `-0.382` (prob 0.68), `-1.158` (prob 0.31). Useful
spread for a low-confidence UI flag.

## Decision

Sprint 1 changes go to production:

1. **`scribe_v2` is now the default model.** Override via
   `ELEVENLABS_STT_MODEL`. The OpenAPI schema requires `scribe_v2`
   for `no_verbatim`, so v2 is also a precondition for any future
   filler-removal experiments.
2. **`temperature=0` and `seed=42`** are now sent by default.
   Re-running the same job on the same audio returns the same
   transcript, which is essential for regression tests.
3. **`language_code` is now plumbed end-to-end** through
   `SttEngine.transcribe(..., language_code=...)`. Callers default to
   `None` (auto-detect) so existing call sites are unchanged.
4. **`logprob` → `WordTimestamp.score`** via
   `score = exp(logprob)`. The previous hardcoded `score=1.0`
   discarded model-supplied confidence; we now expose it for the UI.
5. **`no_verbatim` is opt-in via `ELEVENLABS_NO_VERBATIM`** and only
   sent to `scribe_v2`. Default off because the empirical test
   showed no meaningful filler removal on Japanese audio.

## What was NOT done (and why)

* **`segmented_json` adoption — REJECTED.** The output shape lacks
  segment-level `start`/`end` and contains `text="?"` punctuation-only
  segments (4-second tail-silence artefacts). Adopting it would mean
  re-deriving boundaries from words and adding a punct-only filter —
  which is exactly what `_parse_response` already does, well-tested,
  with no surprises on CJK audio.
* **Lowering `MAX_WORDS_PER_SEGMENT` or dropping the unconditional
  `.?!` split.** The deliberate over-split is what makes our Shorts
  captions readable on small screens; matching ElevenLabs Studio
  segment lengths (10-14 words / segment) would worsen UX in our
  output medium.
* **Dropping `_redistribute_identical_start_clusters`.** The CJK
  zero-duration cluster pathology is not documented as fixed in v2
  and our sample still shows tail-silence anomalies (`か？` spans
  4.74 s in MIKO). The cluster-redistribution sanitiser is cheap
  defence-in-depth; removing it before reproducing 0 occurrences in
  20+ samples would be premature optimisation.
* **`keyterms`.** Worth wiring later but requires a UI change
  (per-Job keyterm list) and a billing-warning surcharge dialog. Out
  of scope for Sprint 1.

## Consequences

* **Net diff**: ~+12 LOC (config), ~+30 LOC (logprob helper +
  payload), ~+115 LOC (new tests). 0 LOC removed. The original audit
  estimate of "−120 LOC" was overoptimistic — the honest tally is
  **+157 LOC, 5 empirically validated improvements**.
* **Reproducibility**: same audio → same words timing → same `score`.
  Any future regression in the rest of the pipeline is now isolatable
  because Phase 1 output is deterministic.
* **Confidence wiring**: the Preview UI can flag `score < 0.5` words
  as low-confidence in a future iteration. The data is now there
  even if the UI doesn't consume it yet.
* **Forward compatibility**: `keyterms`, `entity_detection`,
  `detect_speaker_roles`, and `additional_formats` are all reachable
  from the same payload-assembly seam. Future audits can flip them
  on without re-architecting `_call_api`.

## Test coverage

Added 8 new tests in `tests/test_elevenlabs_stt.py`:

* `TestConfidenceFromLogprob` (5 cases) — covers the `logprob → score`
  helper edge cases.
* `TestPayloadHonoursConfig` (3 cases) — fakes `httpx.AsyncClient`
  to assert that `model_id`, `temperature`, `seed`, `no_verbatim`
  guard, and `language_code` reach the wire.

`tests/test_elevenlabs_stt.py` total: 22 tests. Full STT-pipeline
suite (timing + recheck + translator + transcript_sync + STT) at
84 / 84 passing.

## Audit artifacts retained

The A/B test script and three sample runs are intentionally kept:

```
scripts/audit_segmented_json.py
output/audit_ad4b2c61423d/      # MIKO single-speaker
output/audit_okayu/             # Okayu 2-speaker (verbatim)
output/audit_okayu_noverbatim/  # Okayu 2-speaker (no_verbatim=true)
```

Each `summary.txt` records the side-by-side stats this ADR cites. If
ElevenLabs ships an improved `no_verbatim` for CJK or a fix to the
punctuation-only segment artefact, the same script reruns on the
same audio for an updated comparison.
