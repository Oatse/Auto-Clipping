# ADR-0003: Cut Strategies, Scoring Profiles, Hook Optimizer

**Status:** Accepted (2026-05-26)

**Context:**

The May-26 audit found three orthogonal levers that lift clip quality
without changing the pipeline shape:

1. The `ClipScore.total` weights are tuned for VTuber content. Podcasts,
   news cuts, ASMR, and gaming get scored pessimistically because the
   `retention_hook=0.25` / `emotional_intensity=0.20` bias rewards loud
   spikes more than information density.
2. `Moment.start` is snapped to the nearest silence boundary, but a
   silence boundary is not the same thing as a *hook*. A clip that
   opens on the speaker mid-breath converts worse than one that opens
   on a question or an exclamation, even when the words are identical.
3. Each base time-range produced by Clip Finder turns into exactly one
   Clip. Creators routinely want to see a tight 15 s cut **and** a
   30 s contextual cut for the same Moment, then pick.

**Decision:**

* Add a **Scoring Profile** concept — a named bundle of `ClipScore`
  weights selected per Job. Defaults to `vtuber` so existing callers
  see no behaviour change. Stored on the Job (`scoring_profile`)
  so re-runs are reproducible.
* Add a **Hook Optimizer** as a second pass *inside* boundary
  refinement. Runs after silence-snap, only shifts `Moment.start`
  forward by ≤3 s, and only when a hook-class line (question /
  exclamation / name-drop) is present in the look-ahead window. The
  ADR-0001 timing-source-of-truth contract is unchanged: the
  ElevenLabs words are still canonical; the optimizer only chooses a
  *different* word boundary as the new start.
* Add **Cut Strategies** as a refinement step that can produce
  multiple Moments from the same base time-range. v1 ships three
  named strategies: `tight`, `hooky`, `context`. Each strategy still
  produces 1 Moment → 1 Clip; the only relaxation in CONTEXT is that
  one *base* time-range can fan out into N Moments before hitting the
  cut stage.

**Why these three together:**

They share the same seam — `boundary.refine_boundaries`. Splitting
them across three ADRs would create premature abstractions; bundling
keeps the surgical change set small and the testing scope clear.

**Out of scope for this ADR:**

* Niche-specific *detection* prompts (the Hunters list stays VTuber-
  flavoured for now). Scoring Profile only re-weights, it does not
  re-detect.
* Per-Clip variant rendering in the All In Workspace UI. v1 surfaces
  Cut Strategies as a Clip Finder-only feature; All In keeps its
  preset-per-Job model. Surfacing variants in All In is a v1.1
  decision once the UI grilling is done.
* Cross-Profile A/B comparison. The Job stores one profile; switching
  profiles requires a re-run.

**Consequences:**

* `ClipScore.total(profile)` becomes the single source of truth for
  weighting. The `total` *property* stays as an alias that calls
  `total(profile=ScoringProfile.VTUBER)` so existing callers and
  serialisation keep working.
* `processors/clip_finder/scoring_profiles.py` owns the weight
  tables. New profiles are a one-place change.
* `processors/clip_finder/hook_optimizer.py` is a stateless pass with
  one public function `apply(moments, transcript, *, window_seconds)`
  composed into `boundary.refine_boundaries`.
* `processors/clip_finder/cut_strategies.py` exposes one entry point
  `expand(base_moments, transcript, strategies)` that returns a list
  of derived Moments. The orchestrator runs it after boundary refine,
  before scoring, so each derived Moment gets its own score.

**Test coverage targets:**

* `tests/test_scoring_profiles.py` — every profile sums to 1.0 ±
  determinist contributions; same candidate scores differently under
  vtuber vs podcast.
* `tests/test_hook_optimizer.py` — start snaps forward only when a
  hook line exists in window; never shifts past `min_clip` floor;
  no-op when transcript has no words.
* `tests/test_cut_strategies.py` — `tight` and `hooky` always
  produce a sub-range of the base; `context` never shrinks below
  the base; duplicates are deduplicated against the base.

**Status of legacy paths:**

* `ClipScore.total` (the property) remains as a backward-compat
  alias for `ClipScore.total_for(ScoringProfile.VTUBER)`.
* `boundary.refine_boundaries` keeps its signature; the hook
  optimizer is a strict additional pass inside the same function,
  guarded by a `hook_optimizer=True` kwarg defaulted to `True`.
