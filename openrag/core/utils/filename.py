"""Filename sanitization and generation utilities.

Pure functions — no infrastructure imports.

Extracted from: components/indexer/utils/files.py (pure parts only).
"""

import re
import secrets
import time
from pathlib import Path


def sanitize_filename(filename: str) -> str:
    """Sanitize a filename by removing special characters.

    Keeps only word characters and underscores. Hyphens are converted
    to underscores. Multiple underscores are collapsed.

    Args:
        filename: Original filename (with extension)

    Returns:
        Sanitized filename with extension preserved
    """
    path = Path(filename)
    name = path.stem
    ext = path.suffix

    name = re.sub(r"[^\w\-]", "_", name)
    name = name.replace("-", "_")
    name = re.sub(r"_+", "_", name)
    name = name.strip("_")

    return name + ext


def make_unique_filename(filename: str) -> str:
    """Generate a unique filename by prepending timestamp + random hex.

    Args:
        filename: Original filename

    Returns:
        Unique filename like "1713700000000_a1b2_original.pdf"
    """
    ts = int(time.time() * 1000)
    rand = secrets.token_hex(2)
    return f"{ts}_{rand}_{filename}"
