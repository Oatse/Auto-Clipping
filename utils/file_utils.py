"""
utils/file_utils.py — File system helpers for the pipeline.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import config


def ensure_dir(path: Path | str) -> Path:
    """Create a directory (and parents) if it does not exist. Returns the Path."""
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def temp_path(suffix: str = ".wav", prefix: str = "clip_") -> Path:
    """
    Generate a unique temporary file path inside TEMP_DIR.
    The file is NOT created — only the path is returned.
    """
    ensure_dir(config.TEMP_DIR)
    return config.TEMP_DIR / f"{prefix}{uuid.uuid4().hex}{suffix}"


def segment_output_path(
    output_dir: Path | str,
    segment_index: int,
    stage: str,
    suffix: str = ".wav",
) -> Path:
    """
    Build a deterministic output path for a pipeline segment.

    Example: output/temp/seg_003_elastic.wav
    """
    output_dir = Path(output_dir)
    ensure_dir(output_dir)
    return output_dir / f"seg_{segment_index:03d}_{stage}{suffix}"


def safe_stem(text: str, max_len: int = 40) -> str:
    """
    Convert arbitrary text to a safe filename stem.
    Strips non-alphanumeric characters and truncates.
    """
    import re
    safe = re.sub(r"[^\w\-]", "_", text)
    return safe[:max_len].strip("_")
