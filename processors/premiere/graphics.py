"""
processors/premiere/graphics.py — Captions as Essential Graphics clips.

Importing an SRT gives a caption track, which is rigid: one cue at a time,
limited styling, and no way to show two people talking at once. Premiere's own
answer is "Upgrade Caption to Graphic", turning each cue into a text clip that
can be restyled, animated and moved freely.

That menu command is not scriptable — Premiere's ExtendScript exposes no
caption API, no ``performAction``, and ``qe.executeConsoleCommand`` rejects
command names. But the *result* can be built directly, which is better anyway:

    sequence.importMGT(template, ticks, videoTrack, audioTrack)  -> Graphic clip
    clip.components["Text"].properties[0].setValue(text)          -> its words
    clip.end = <Time>                                             -> its timing

All three were verified against Premiere 2023 before this module was written.

The payoff is not only flexibility. Graphics are ordinary clips, so putting
overlapping cues on **separate video tracks** lets simultaneous speech appear
simultaneously — the thing SRT fundamentally cannot do, and the reason merged
cues have to share one set of timings.

Premiere ships the templates this uses (Essential Graphics > Captions and
Subtitles), so nothing has to be authored in After Effects.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from models.transcript import TranscriptSegment

from .bridge_client import BridgeResponse, PremiereBridge, _escape

LogFn = Callable[[str], None]

# Premiere counts time in ticks.
TICKS_PER_SECOND = 254016000000

DEFAULT_TEMPLATE_NAME = "Simple Web Caption.mogrt"
_TEMPLATE_ROOTS = (r"C:\Program Files\Adobe", "/Applications")

# Cap on how many tracks overlapping speech may spread across. Beyond this the
# extra voices stack on the last lane rather than growing the sequence without
# bound.
MAX_LANES = 4

# How many graphics to place per bridge call.
#
# This is a stability limit, not a tuning knob. Every importMGT re-reads and
# unpacks the .mogrt from disk (roughly 800 KB), and doing a few hundred of
# them inside one blocking ExtendScript call crashed Premiere outright.
# Batching lets it finish, release, and redraw between groups, and turns a
# failure into "batch 7 of 12 failed" instead of a lost session.
BATCH_SIZE = 20

# Hard ceiling on how many graphics this will place.
#
# Set from evidence, not caution. Placing 276 captions took Premiere down even
# when split into 14 batches: every importMGT unpacks the ~800 KB template and
# adds a project item, so a full timeline means hundreds of megabytes of
# unpacking and hundreds of items. Batching bought nothing.
#
# So this path is for a short section, not a finished compilation. For a whole
# timeline, import a caption track and use Premiere's own
# Captions > Upgrade Caption to Graphic, which converts the lot in one native
# operation instead of importing a template hundreds of times.
SAFE_LIMIT = 40


@dataclass
class GraphicCue:
    """One caption as it will exist on the timeline."""

    start: float
    end: float
    text: str
    lane: int = 0


def find_caption_template(explicit: str | None = None) -> Path | None:
    """Locate a bundled caption Motion Graphics Template.

    Searched by name under the Adobe install root rather than a fixed path,
    since that path carries the version year and would break on upgrade.
    """
    if explicit and Path(explicit).is_file():
        return Path(explicit)

    from_env = os.getenv("PREMIERE_CAPTION_MOGRT")
    if from_env and Path(from_env).is_file():
        return Path(from_env)

    for root in _TEMPLATE_ROOTS:
        base = Path(root)
        if not base.is_dir():
            continue
        try:
            exact = list(base.glob(f"**/Captions and Subtitles/{DEFAULT_TEMPLATE_NAME}"))
            if exact:
                return exact[0]
            any_caption = list(base.glob("**/Captions and Subtitles/*.mogrt"))
            if any_caption:
                return sorted(any_caption)[0]
        except OSError:
            continue
    return None


def assign_lanes(
    segments: Sequence[TranscriptSegment],
    *,
    max_lanes: int = MAX_LANES,
) -> list[GraphicCue]:
    """Lay cues out so overlapping ones land on different tracks.

    Greedy interval partitioning: a cue takes the first lane whose previous
    cue has already ended. Non-overlapping speech therefore stays on one
    track — the common case — and only genuine simultaneous speech spreads
    upward, which is exactly when a second track is wanted.
    """
    ordered = sorted(
        (s for s in segments if (s.text or "").strip()),
        key=lambda s: (s.start, s.end),
    )
    lane_ends: list[float] = []
    cues: list[GraphicCue] = []

    for segment in ordered:
        start = float(segment.start)
        end = float(max(segment.end, segment.start))
        if end <= start:
            end = start + 0.2          # a zero-length clip is invisible

        lane = None
        for index, lane_end in enumerate(lane_ends):
            if start >= lane_end - 1e-6:
                lane = index
                break
        if lane is None:
            if len(lane_ends) < max_lanes:
                lane_ends.append(end)
                lane = len(lane_ends) - 1
            else:
                # Out of lanes: stack on the last one and let it overwrite,
                # rather than growing the sequence indefinitely.
                lane = max_lanes - 1
                lane_ends[lane] = end
        else:
            lane_ends[lane] = end

        cues.append(
            GraphicCue(start=start, end=end, text=(segment.text or "").strip(), lane=lane)
        )
    return cues


def _prepare_tracks_script(lanes: int) -> str:
    """Add the graphics tracks once, before any cue is placed."""
    return f"""
var seq = app.project.activeSequence;
if (!seq) {{ return '{{"success":false,"error":"No sequence is open"}}'; }}
var baseTrack = seq.videoTracks.numTracks;
app.enableQE();
try {{ qe.project.getActiveSequence().addTracks({lanes}, baseTrack, 0, 0); }} catch (e) {{}}
return '{{"success":true,"data":{{"baseTrack":' + baseTrack +
       ',"available":' + seq.videoTracks.numTracks + '}}}}';
"""


def _place_batch_script(cues: Sequence[GraphicCue], mogrt: Path, base_track: int) -> str:
    """Place one batch of cues. Kept small on purpose — see BATCH_SIZE."""
    payload = json.dumps(
        [{"s": c.start, "e": c.end, "t": c.text, "l": c.lane} for c in cues],
        ensure_ascii=False,
    )
    return f"""
var CUES = {payload};
var MOGRT = "{_escape(str(mogrt))}";
var TICKS = {TICKS_PER_SECOND};
var BASE = {base_track};

var seq = app.project.activeSequence;
if (!seq) {{ return '{{"success":false,"error":"No sequence is open"}}'; }}
var available = seq.videoTracks.numTracks;
var placed = 0, failed = 0;

for (var i = 0; i < CUES.length; i++) {{
  var cue = CUES[i];
  var trackIndex = BASE + cue.l;
  if (trackIndex >= available) {{ trackIndex = available - 1; }}

  var ticks = Math.round(cue.s * TICKS);
  var clip = null;
  try {{ clip = seq.importMGT(MOGRT, ticks.toString(), trackIndex, 0); }} catch (e) {{ clip = null; }}
  if (!clip) {{ failed++; continue; }}

  try {{
    var comps = clip.components, text = null;
    for (var k = 0; k < comps.numItems; k++) {{
      if (comps[k].displayName === 'Text') {{ text = comps[k]; break; }}
    }}
    if (text) {{ text.properties[0].setValue(cue.t, true); }}
  }} catch (e) {{}}

  try {{
    var endTime = clip.end;
    endTime.seconds = cue.e;
    clip.end = endTime;
  }} catch (e) {{}}

  placed++;
}}
return '{{"success":true,"data":{{"placed":' + placed + ',"failed":' + failed + '}}}}';
"""


def import_as_graphics(
    bridge: PremiereBridge,
    segments: Sequence[TranscriptSegment],
    *,
    template: Path | None = None,
    max_lanes: int = MAX_LANES,
    batch_size: int = BATCH_SIZE,
    limit: int | None = SAFE_LIMIT,
    timeout: float = 300.0,
    log_fn: LogFn | None = None,
) -> BridgeResponse:
    """Place every caption on the timeline as an Essential Graphics clip.

    Placed in batches rather than one long call. Each ``importMGT`` re-reads
    and unpacks the template from disk, and a few hundred of them inside one
    blocking script crashed Premiere. Batching lets it finish and redraw
    between groups, and localises a failure instead of losing the run.
    """
    mogrt = template or find_caption_template()
    if mogrt is None:
        return BridgeResponse.failed(
            "No caption template found. Premiere ships these under Essential "
            "Graphics > Captions and Subtitles; set PREMIERE_CAPTION_MOGRT to "
            "one if yours is installed elsewhere."
        )

    cues = assign_lanes(segments, max_lanes=max_lanes)
    if not cues:
        return BridgeResponse.failed("no captions to place")

    if limit is not None and len(cues) > limit:
        return BridgeResponse.failed(
            f"{len(cues)} captions is too many to place as graphics (limit "
            f"{limit}). Importing the template once per caption crashes "
            "Premiere at this volume, batched or not. Use the caption track "
            "instead, then Captions > Upgrade Caption to Graphic in Premiere — "
            "it converts them all in one native operation."
        )

    lanes_used = max((c.lane for c in cues), default=0) + 1
    if log_fn:
        log_fn(
            f"Placing {len(cues)} caption graphic(s) across {lanes_used} track(s)"
            + (" so overlapping speech can show together" if lanes_used > 1 else "")
        )

    prepared = bridge.execute(_prepare_tracks_script(lanes_used), timeout=120.0)
    if not prepared.success:
        return BridgeResponse.failed(f"could not add tracks: {prepared.error}")
    base_track = int((prepared.data or {}).get("baseTrack", 0))

    placed = failed = 0
    batches = [cues[i:i + batch_size] for i in range(0, len(cues), batch_size)]
    for number, batch in enumerate(batches, start=1):
        response = bridge.execute(
            _place_batch_script(batch, mogrt, base_track), timeout=timeout,
        )
        if not response.success:
            # Stop rather than keep hammering an unhappy Premiere, and report
            # how far it got so the partial result is understandable.
            return BridgeResponse.failed(
                f"stopped at batch {number} of {len(batches)} after placing "
                f"{placed} graphic(s): {response.error}"
            )
        placed += int((response.data or {}).get("placed", 0))
        failed += int((response.data or {}).get("failed", 0))
        if log_fn and len(batches) > 1:
            log_fn(f"  {placed}/{len(cues)} placed")

    return BridgeResponse(
        success=True,
        data={
            "placed": placed,
            "failed": failed,
            "tracks": lanes_used,
            "baseTrack": base_track,
            "batches": len(batches),
        },
    )


__all__ = [
    "GraphicCue",
    "assign_lanes",
    "import_as_graphics",
    "find_caption_template",
    "TICKS_PER_SECOND",
    "MAX_LANES",
]
