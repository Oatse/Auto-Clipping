"""
processors.translator.postprocess — Translation output post-processing.

Currently exposes one capability:

* :func:`apply_soft_censor` — defense-in-depth regex pass that swaps a
  curated list of vulgar English terms for playful equivalents
  (e.g. "dick" → "wiener", "cumming" → "finishing"). Used as the second
  layer of the **spicy filter**: the first layer is a prompt instruction
  asking the model to prefer playful equivalents. This regex pass catches
  the cases where the model ignored the instruction and translated
  literally anyway.

Design notes:

* All matches use ``\\b`` word boundaries so we don't mangle innocent
  substrings (no "dictionary" → "wienerionary").
* Case is preserved by the replacement function: "DICK" → "WIENER",
  "Dick" → "Wiener", "dick" → "wiener", "diCk" → "wiener" (mixed case
  collapses to lowercase — acceptable for subtitle output).
* The map is curated — only catches obvious R18 / shimoneta vocabulary.
  Mild profanity (``damn``, ``crap``, ``hell``) is intentionally NOT in
  the list because it's already subtitle-appropriate.
* Multi-word phrases are matched first so "i'm cumming" replaces before
  "cumming" alone. That ordering matters for casing: ``I'm finishing``
  vs ``I'm Finishing``.
"""

from __future__ import annotations

import re
from typing import Final

# ── Curated vulgar → playful map ────────────────────────────────────────
#
# Order matters: longer / multi-word phrases first so they win against
# the single-word patterns below.
_RAW_MAP: Final[list[tuple[str, str]]] = [
    # Sexual climax phrases — multi-word first.
    (r"\bi(?:'m| am)\s+(?:gonna\s+)?cum(?:ming)?\b", "I'm finishing"),
    (r"\bgonna\s+cum\b", "gonna finish"),
    (r"\bcumming\b", "finishing"),
    (r"\bcum\b", "finish"),

    # Genitals — male.
    (r"\b(?:dick|cock|penis|prick|johnson|schlong)s?\b", "wiener"),

    # Genitals — female.
    (r"\b(?:pussy|cunt|twat)\b", "lady-bits"),
    (r"\bpussies\b", "lady-bits"),

    # Chest. "Boobs" itself is already mild enough to keep, but vulgar
    # synonyms get rounded down to "boobies".
    (r"\btits\b", "boobies"),
    (r"\bjugs\b", "boobies"),

    # Promiscuity / horniness.
    (r"\bhorn(?:y|ee+|i+)\b", "naughty"),
    (r"\bsluts?\b", "naughty one"),
    (r"\bbitch(?:es)?\b", "meanie"),

    # Sex acts / explicit verbs that bowdlerize cleanly.
    (r"\bblow\s*job\b", "BJ"),
    (r"\bhand\s*job\b", "HJ"),
    (r"\bjerk(?:ing)?\s+off\b", "rubbing one out"),

    # Mild profanity that VTuber subs usually still want softened
    # — opt-in via `spicy_filter`, not a tone change.
    (r"\bfuck(?:ing|ed|s)?\b", "freaking"),
    (r"\bsh[i*]ts?\b", "crap"),
    (r"\basshole?s?\b", "jerk"),
]


# Pre-compile patterns once at import time. We compile case-insensitive
# so a single pattern matches all four casings (UPPER, Title, lower, mixed).
_COMPILED: Final[list[tuple[re.Pattern[str], str]]] = [
    (re.compile(pat, re.IGNORECASE), repl) for pat, repl in _RAW_MAP
]


def _preserve_case(matched: str, replacement: str) -> str:
    """Adjust ``replacement`` to mirror ``matched``'s casing pattern.

    Three rules, in order:

    1. ``matched`` is all UPPERCASE → ALL-UPPERCASE replacement.
    2. ``matched`` starts with an uppercase letter → Capitalize replacement.
    3. Otherwise (lowercase or mixed) → leave replacement as-is.
    """
    if not matched or not replacement:
        return replacement

    # Strip whitespace for the casing check (multi-word phrases like
    # "I'm cumming" → "I'm finishing" should respect the I).
    leading = matched.lstrip()
    if not leading:
        return replacement

    if leading.isupper() and len(leading.replace(" ", "")) > 1:
        return replacement.upper()
    if leading[0].isupper():
        # Capitalize first letter, leave rest as-defined.
        return replacement[0].upper() + replacement[1:]
    return replacement


def apply_soft_censor(text: str) -> str:
    """Soften explicit English vocabulary in ``text``.

    Idempotent: running twice produces the same result as running once
    because the playful targets ("wiener", "naughty", "finishing", etc.)
    are not themselves matched by any pattern.

    Returns the (possibly mutated) string. Always returns a string —
    never ``None`` — even when the input is empty.
    """
    if not text:
        return text or ""

    out = text
    for pattern, replacement in _COMPILED:
        out = pattern.sub(
            lambda m, r=replacement: _preserve_case(m.group(0), r),
            out,
        )
    return out


def apply_soft_censor_many(lines: list[str]) -> list[str]:
    """Vectorised convenience wrapper for :func:`apply_soft_censor`."""
    return [apply_soft_censor(s) for s in lines]


__all__ = [
    "apply_soft_censor",
    "apply_soft_censor_many",
]
