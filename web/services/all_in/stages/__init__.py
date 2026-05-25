"""
web.services.all_in.stages — Single-purpose pipeline stages.

Each stage exposes one async function with a small, context-free
signature (input paths, output dir, options) and returns a typed
result.  The runner composes them.

This boundary is the same boundary the future service-layer refactor
(ADR-0002) will use.  Each ``stages/{name}.py`` is a self-contained
unit that can move to ``web/services/{name}_service.py`` without a
signature change.
"""

from __future__ import annotations
