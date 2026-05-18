"""
web.services — Backend services consumed by FastAPI route handlers.

Splitting reduces the size of ``web/server.py`` (formerly 2050 LOC) and
gives each piece a clear responsibility:

* :mod:`.job_models`       — Job / JobStatus / PHASE_LABELS data
* :mod:`.transcript_sync`  — Sync segment.words[] with edited text
* :mod:`.pipeline_runner`  — Run the 4-phase auto-subtitle pipeline
"""
