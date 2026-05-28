# ADR-0005: Score Profile travels with the Clip

**Status:** Accepted (2026-05-28)

**Context:**

ADR-0003 added Scoring Profile as a Job-level concept, re-weighting
``ClipScore.total`` per content niche. The implementation stamped the
profile-aware total onto each Clip as a sidecar attribute
(``_profile_total``) and kept ``ClipScore.total`` as a VTuber-default
property per the ADR-0003 backward-compat contract.

The May-28 audit found three downstream consumers that never read the
sidecar and silently fell back to VTuber weights:

1. ``selection.select_top_clips`` sorts by ``c.score.total`` — so the
   diversified top-N picked under a podcast or news Job is actually
   ranked under VTuber weights.
2. ``Clip.to_dict()`` emits ``score.total`` from the same VTuber
   property — the UI renders VTuber scores even when the Job is
   podcast.
3. The orchestrator only set the sidecar when ``profile != VTUBER``,
   so even within Python the attribute was missing on the default path.

A profile that doesn't propagate to ranking and serialisation is not
really a profile.

**Decision:**

Promote profile from a sidecar attribute to a real ``Clip`` field:
``Clip.score_profile: str``. The orchestrator's
``_apply_scoring_profile`` step stamps every Clip with the Job's
profile string before selection and serialisation.

* ``Clip.to_dict()`` overwrites ``score["total"]`` using
  ``ClipScore.total_for(self.score_profile)`` — JSON sent to the UI
  is profile-aware.
* ``Clip.from_dict()`` round-trips the field so persisted Jobs
  rehydrate with the original profile.
* ``select_top_clips`` accepts an optional ``profile`` parameter
  (defaults to each Clip's own ``score_profile``) and ranks
  accordingly.
* ``ClipScore`` is unchanged. The ADR-0003 contract that
  ``ClipScore.total`` remains a VTuber-default alias is preserved.

**Why this lives on Clip rather than mutating ClipScore:**

* Mutating ``ClipScore.total`` would break ADR-0003. We need a place
  to store the resolved profile that doesn't fight the existing
  contract.
* ``ClipScore`` is meant to be shareable across profiles — same
  rubric, different weights. Stamping the profile on the *Clip*
  keeps ``ClipScore`` reusable for A/B comparison work later.
* The Clip is the natural unit of UI display; the UI never sees the
  raw ClipScore detached from its Clip, so the field is always at
  hand at the point of rendering.

**Trade-offs:**

* Every Clip in a Job repeats the same profile string. Acceptable —
  it's <16 bytes per Clip and Jobs typically hold ≤20 Clips.
* If a future feature wants per-Clip profile (e.g. cross-profile A/B
  on the same time-range) the data shape already supports it. ADR
  ADR-0003 explicitly lists that as out of scope for v1; this ADR
  doesn't open it, just doesn't preclude it.
* Single-shot now also accepts an optional ``max_count`` and runs
  ``select_top_clips`` when set. Default behaviour (return everything)
  is preserved by keeping ``max_count=None`` as the default — see the
  May-28 audit "Bug #2" note.

**Consequences:**

* ``models/clip.py`` gains the ``score_profile`` field and round-trip
  support in ``to_dict`` / ``from_dict``.
* ``processors/clip_finder/selection.py`` exposes a ``profile``
  kwarg and reads ``Clip.score_profile`` as fallback.
* ``processors/clip_finder/orchestrator.py`` stamps the field on
  every clip and forwards profile into ``select_top_clips``. The
  ``_profile_total`` sidecar attribute is removed.
* ``web/routes/clip_finder.py`` log line uses ``total_for(score_profile)``.
* ``tests/test_scoring_profiles.py`` keeps the ADR-0003 invariants
  (``total_for(VTUBER) == total``); ``tests/test_clip_finder.py``
  adds coverage for profile-aware sorting.

**Rejected alternatives:**

* *Mutate ``ClipScore.total``*: breaks ADR-0003.
* *Pass profile at every call site*: orchestrator would need to
  forward profile to every consumer, including the UI's JSON
  consumer that has no Python context. Brittle.
