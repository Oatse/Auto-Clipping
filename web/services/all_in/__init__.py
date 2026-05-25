"""
web.services.all_in — All In Workspace orchestrator.

The All In Workspace (Workspace · 04) chains Clip Finder's moment
detection, the auto-subtitle pipeline, and Short Maker's reframe
into a single hands-off Job that produces finished, captioned,
reframed Clips from a YouTube URL.

This package follows the B-shaped layout described in
``docs/adr/0002-all-in-orchestrator-pattern.md`` so a future refactor
to a shared service layer is rename-and-relocate, not redesign.

Public entry points:
    - run_all_in_job(job)              — top-level orchestrator
    - retry_clip(job, clip_idx)        — re-render a single failed clip

Submodules:
    - models                           — AllInJob, AllInClip, enums
    - presets                          — Bold / Minimal / Karaoke caption presets
    - runner                           — orchestrator implementation
    - stages.source                    — full source video download
    - stages.moments                   — Gemini moment detection adapter
    - stages.cut                       — range cut + silence trim
    - stages.reframe                   — smart-static crop + reframe
    - stages.caption                   — auto-subtitle adapter
"""

from __future__ import annotations
