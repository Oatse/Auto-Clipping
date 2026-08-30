"""
processors/premiere — Handoff from Clip Finder to Adobe Premiere Pro.

Compilation mode does not cut dozens of files. It downloads ONE master
VOD and describes the selected moments as in/out points on that master,
so the editor opens a timeline holding only the good parts while the
source stays whole and re-trimmable.

Modules:
    source    Master video / audio acquisition + ffprobe media metadata.
    fcpxml    Final Cut Pro 7 XML timeline generation (Premiere imports
              this natively via File > Import, and the MCP bridge can
              import the same artifact for one-click handoff).
    pipeline  One URL to a finished timeline, running analysis and the
              master download concurrently.
"""

from .fcpxml import build_fcpxml, write_fcpxml
from .pipeline import CompilationResult, build_compilation
from .source import MasterSource, MediaInfo, probe_media

__all__ = [
    "MediaInfo",
    "MasterSource",
    "probe_media",
    "build_fcpxml",
    "write_fcpxml",
    "build_compilation",
    "CompilationResult",
]
