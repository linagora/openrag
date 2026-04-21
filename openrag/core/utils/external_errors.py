"""Utilities for detecting external resource access errors.

When VLM models fetch external image URLs, HTTP errors (403, 404, etc.)
from remote servers get wrapped in InternalServerError, which is
misleading. This module detects such errors for better logging.

Pure functions — no infrastructure imports.

Moved from: utils/external_resource_errors.py
"""

import re

EXTERNAL_ERROR_CODES = frozenset(
    {
        # 4xx client errors
        "400", "401", "403", "404", "405", "408", "410", "429", "451",
        # 5xx gateway errors
        "502", "503", "504",
    }
)

EXTERNAL_ERROR_INDICATORS = (
    "ClientResponseError",
    "HTTPError",
    "ConnectionError",
    "TimeoutError",
    "SSLError",
)


def is_external_resource_error(error: Exception) -> tuple[bool, str, str]:
    """Check if an error is caused by an external resource access issue.

    Returns:
        (is_external_error, status_code, url) — status_code and url are
        empty strings if not detected.
    """
    error_str = str(error)

    status_code = ""
    for match in re.finditer(r"\b([45]\d{2})\b", error_str):
        if match.group(1) in EXTERNAL_ERROR_CODES:
            status_code = match.group(1)
            break

    url_match = re.search(r"https?://[^\s'\"\)>]+", error_str)
    url = url_match.group(0) if url_match else ""

    has_indicator = any(ind in error_str for ind in EXTERNAL_ERROR_INDICATORS)

    return bool(status_code) or has_indicator, status_code, url
