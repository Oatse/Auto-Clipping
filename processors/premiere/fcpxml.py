"""
processors/premiere/fcpxml.py — Final Cut Pro 7 XML timeline generation.

Premiere imports FCP7 XML natively (File > Import), and the MCP bridge's
``import_fcp_xml`` consumes the same file — so one artifact serves both the
manual and the one-click handoff.

The timeline references a single master file and places each selected
moment as a clip with source in/out points. Nothing is re-encoded and the
master is never cut: the editor gets a timeline of just the good parts
while every frame stays available for re-trimming.

Frame accuracy is the whole game here. Seconds are converted to frames
against the master's exact rational frame rate, and broadcast rates are
written the way FCP7 XML expects them — an integer ``timebase`` plus an
``ntsc`` flag (29.97 is ``timebase 30`` + ``ntsc TRUE``), never a rounded
decimal.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path
from typing import Iterable, Sequence
from xml.dom import minidom

from models.clip import Clip

from .source import MediaInfo

_DOCTYPE = "<!DOCTYPE xmeml>"


# ─── Frame math ──────────────────────────────────────────────────────────────


def seconds_to_frames(seconds: float, fps: Fraction) -> int:
    """Convert seconds to a whole frame index at ``fps``.

    Uses the exact rational rate so a two-hour timeline does not drift:
    at 29.97 fps, rounding to 30 would slip roughly 3.6 seconds by the
    end of the VOD.
    """
    if seconds <= 0:
        return 0
    return int(round(seconds * float(fps)))


def _rate_element(parent: ET.Element, info: MediaInfo) -> ET.Element:
    """Write the ``<rate>`` block FCP7 XML expects."""
    rate = ET.SubElement(parent, "rate")
    ET.SubElement(rate, "timebase").text = str(info.timebase)
    ET.SubElement(rate, "ntsc").text = "TRUE" if info.is_ntsc else "FALSE"
    return rate


def path_to_url(path: Path) -> str:
    """Absolute ``file://localhost/...`` URL for a media path.

    Premiere relinks by this URL, so it must be absolute. Keeping the
    master and the .xml side by side means a moved project relinks in one
    step instead of per clip.
    """
    resolved = path.resolve()
    text = str(resolved).replace("\\", "/")
    if not text.startswith("/"):
        text = "/" + text          # Windows drive letters: /D:/path
    from urllib.parse import quote

    # ``:`` and ``/`` must stay literal — percent-encoding the drive-letter
    # colon (``/D%3A/...``) produces a URL Premiere cannot relink. Spaces
    # and other characters are still escaped.
    return "file://localhost" + quote(text, safe="/:")


# ─── Timeline construction ───────────────────────────────────────────────────


def build_fcpxml(
    clips: Sequence[Clip],
    master: MediaInfo,
    *,
    sequence_name: str = "Clip Finder Compilation",
    source_url: str = "",
) -> str:
    """Return an FCP7 XML document placing ``clips`` on one timeline.

    Clips are laid end to end with no gaps (jump-cut assembly); transitions
    are left to the editor. Each moment occupies one video clipitem plus a
    linked audio clipitem, so selecting one selects both in Premiere.

    ``source_url`` is embedded as a sequence comment to satisfy the
    attribution requirement for VTuber clip channels — the original
    stream URL travels with the timeline rather than living only in the
    job record.
    """
    ordered = sorted(clips, key=lambda c: c.start)

    xmeml = ET.Element("xmeml", {"version": "4"})
    sequence = ET.SubElement(xmeml, "sequence", {"id": "sequence-1"})
    ET.SubElement(sequence, "name").text = sequence_name

    total_frames = sum(
        _clip_frame_span(clip, master.fps) for clip in ordered
    )
    ET.SubElement(sequence, "duration").text = str(total_frames)
    _rate_element(sequence, master)
    if source_url:
        ET.SubElement(sequence, "comments").text = f"Source: {source_url}"

    media = ET.SubElement(sequence, "media")
    video = ET.SubElement(media, "video")
    _video_format(video, master)
    video_track = ET.SubElement(video, "track")

    audio = ET.SubElement(media, "audio")
    audio_track = ET.SubElement(audio, "track")

    timeline_frame = 0
    for index, clip in enumerate(ordered, start=1):
        span = _clip_frame_span(clip, master.fps)
        if span <= 0:
            continue
        in_frame = seconds_to_frames(clip.start, master.fps)
        out_frame = in_frame + span

        # The file element is fully described once and referenced by id
        # afterwards — the FCP7 XML idiom, and it keeps a 60-clip timeline
        # from repeating the master's metadata 60 times.
        define_file = index == 1

        _clipitem(
            video_track,
            clip=clip,
            index=index,
            master=master,
            timeline_start=timeline_frame,
            in_frame=in_frame,
            out_frame=out_frame,
            media_type="video",
            define_file=define_file,
        )
        _clipitem(
            audio_track,
            clip=clip,
            index=index,
            master=master,
            timeline_start=timeline_frame,
            in_frame=in_frame,
            out_frame=out_frame,
            media_type="audio",
            define_file=False,
        )
        timeline_frame += span

    return _serialise(xmeml)


def _clip_frame_span(clip: Clip, fps: Fraction) -> int:
    """Length of a clip in frames (at least one frame when non-empty)."""
    start = seconds_to_frames(clip.start, fps)
    end = seconds_to_frames(clip.end, fps)
    return max(0, end - start)


def _video_format(video: ET.Element, master: MediaInfo) -> None:
    fmt = ET.SubElement(video, "format")
    chars = ET.SubElement(fmt, "samplecharacteristics")
    _rate_element(chars, master)
    ET.SubElement(chars, "width").text = str(master.width)
    ET.SubElement(chars, "height").text = str(master.height)
    ET.SubElement(chars, "pixelaspectratio").text = "square"


def _clipitem(
    track: ET.Element,
    *,
    clip: Clip,
    index: int,
    master: MediaInfo,
    timeline_start: int,
    in_frame: int,
    out_frame: int,
    media_type: str,
    define_file: bool,
) -> None:
    """Append one clipitem (video or audio) to ``track``."""
    item_id = f"clipitem-{media_type}-{index}"
    item = ET.SubElement(track, "clipitem", {"id": item_id})
    ET.SubElement(item, "name").text = clip.title or f"Moment {index}"
    ET.SubElement(item, "duration").text = str(out_frame - in_frame)
    _rate_element(item, master)
    ET.SubElement(item, "start").text = str(timeline_start)
    ET.SubElement(item, "end").text = str(timeline_start + (out_frame - in_frame))
    ET.SubElement(item, "in").text = str(in_frame)
    ET.SubElement(item, "out").text = str(out_frame)

    file_el = ET.SubElement(item, "file", {"id": "file-master"})
    if define_file:
        ET.SubElement(file_el, "name").text = master.path.name
        ET.SubElement(file_el, "pathurl").text = path_to_url(master.path)
        _rate_element(file_el, master)
        if master.duration > 0:
            ET.SubElement(file_el, "duration").text = str(
                seconds_to_frames(master.duration, master.fps)
            )
        file_media = ET.SubElement(file_el, "media")
        fmv = ET.SubElement(file_media, "video")
        fmv_chars = ET.SubElement(fmv, "samplecharacteristics")
        ET.SubElement(fmv_chars, "width").text = str(master.width)
        ET.SubElement(fmv_chars, "height").text = str(master.height)
        fma = ET.SubElement(file_media, "audio")
        ET.SubElement(fma, "channelcount").text = "2"

    if media_type == "audio":
        source_track = ET.SubElement(item, "sourcetrack")
        ET.SubElement(source_track, "mediatype").text = "audio"
        ET.SubElement(source_track, "trackindex").text = "1"
    else:
        source_track = ET.SubElement(item, "sourcetrack")
        ET.SubElement(source_track, "mediatype").text = "video"
        ET.SubElement(source_track, "trackindex").text = "1"

    # Link video and audio so the editor moves them together.
    for linked_type in ("video", "audio"):
        link = ET.SubElement(item, "link")
        ET.SubElement(link, "linkclipref").text = f"clipitem-{linked_type}-{index}"
        ET.SubElement(link, "mediatype").text = linked_type
        ET.SubElement(link, "trackindex").text = "1"
        ET.SubElement(link, "clipindex").text = str(index)

    # Why this moment was chosen travels with the timeline, so the editor
    # can judge a clip without going back to the job record.
    note = _clip_note(clip)
    if note:
        ET.SubElement(item, "comments").text = note


def _clip_note(clip: Clip) -> str:
    parts: list[str] = []
    total = clip.score.total_for(getattr(clip, "score_profile", "vtuber"))
    parts.append(f"score {total:.2f}/10")
    if clip.highlight_type.value:
        parts.append(clip.highlight_type.value)
    if clip.reason:
        parts.append(clip.reason)
    return " | ".join(parts)


def _serialise(root: ET.Element) -> str:
    """Pretty-print with the xmeml DOCTYPE Premiere expects."""
    raw = ET.tostring(root, encoding="unicode")
    pretty = minidom.parseString(raw).toprettyxml(indent="  ")
    body = pretty.split("\n", 1)[1]  # drop minidom's XML declaration
    return f'<?xml version="1.0" encoding="UTF-8"?>\n{_DOCTYPE}\n{body}'


def write_fcpxml(
    clips: Sequence[Clip],
    master: MediaInfo,
    out_path: Path,
    *,
    sequence_name: str = "Clip Finder Compilation",
    source_url: str = "",
) -> Path:
    """Write the timeline next to the master and return the path."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    xml = build_fcpxml(
        clips, master, sequence_name=sequence_name, source_url=source_url,
    )
    out_path.write_text(xml, encoding="utf-8")
    return out_path


__all__ = ["build_fcpxml", "write_fcpxml", "seconds_to_frames", "path_to_url"]
