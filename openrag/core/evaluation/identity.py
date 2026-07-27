"""Filename ↔ ``file_id`` normalisation shared by the runner and the metrics.

The indexing API accepts only ``[A-Za-z0-9._:-]`` in a ``file_id``, but corpus
filenames routinely contain spaces and accents. The runner therefore uploads a
sanitised id, while a test set names its ground truth by real filename — so
both sides of the ranking comparison are normalised through the same function
rather than hoping the stored metadata preserved the original name.
"""

from __future__ import annotations

import re

_DISALLOWED = re.compile(r"[^A-Za-z0-9._:-]")


def sanitize_file_id(filename: str) -> str:
    """Map a corpus filename onto an id the indexing API accepts."""
    return _DISALLOWED.sub("_", filename)


__all__ = ["sanitize_file_id"]
