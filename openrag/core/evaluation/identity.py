"""Filename to ``file_id`` normalisation, shared by the runner and the metrics.

The indexing API accepts only ``[A-Za-z0-9._:-]`` in a ``file_id``, while a test
set names its ground truth by real filename. Both sides go through this function
so the two still match.
"""

from __future__ import annotations

import re

_DISALLOWED = re.compile(r"[^A-Za-z0-9._:-]")


def sanitize_file_id(filename: str) -> str:
    """Map a corpus filename onto an id the indexing API accepts."""
    return _DISALLOWED.sub("_", filename)


__all__ = ["sanitize_file_id"]
