"""
launcher — One entry point that brings the whole workspace up.

Starts (or adopts) the Auto-Clipping app, Premiere Pro, and the Premiere
bridge, then reports what is available. Every component is probed before
anything is launched, so running the launcher twice adopts what is already
alive instead of duplicating it.

    python -m launcher            # start everything and open the app
    python -m launcher --status   # report only, start nothing

Modules:
    components    Generic "is it alive / make it alive" guards.
    bridge        Premiere automation reachability (separate from the process).
    orchestrator  Component order, options, and the resulting report.
    singleton     Cross-process guard against two launchers racing.
"""

from .bridge import BridgeConfig, bridge_alive, ensure_bridge
from .components import ComponentStatus, State
from .orchestrator import LaunchOptions, LaunchReport, find_premiere_exe, launch
from .singleton import AlreadyRunning, single_instance

__all__ = [
    "launch",
    "LaunchOptions",
    "LaunchReport",
    "find_premiere_exe",
    "ComponentStatus",
    "State",
    "BridgeConfig",
    "bridge_alive",
    "ensure_bridge",
    "single_instance",
    "AlreadyRunning",
]
