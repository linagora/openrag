"""
Utilities for detecting external resource access errors.

When VLM models fetch external image URLs, HTTP errors (403, 404, etc.) from
remote servers get wrapped in InternalServerError, which is misleading.
This module detects such errors for better logging.
"""

import re

# HTTP error codes indicating external resource issues
EXTERNAL_ERROR_CODES = frozenset(
    {
        # 4xx client errors
        "400", "401", "403", "404", "405", "408", "410", "429", "451",
        # 5xx gateway errors
        "502", "503", "504",
    }
)

# Error type indicators for external fetch failures
EXTERNAL_ERROR_INDICATORS = (
    "ClientResponseError",
    "HTTPError",
    "ConnectionError",
    "TimeoutError",
    "SSLError",
)


def is_external_resource_error(error: Exception) -> tuple[bool, str, str]:
    """
    Check if an error is caused by an external resource access issue.

    Returns:
        (is_external_error, status_code, url) - status_code and url are empty
        strings if not detected.
    """
    error_str = str(error)

    # Find HTTP 4xx/5xx status code (first one that's in our allowed set)
    status_code = ""
    for match in re.finditer(r"\b([45]\d{2})\b", error_str):
        if match.group(1) in EXTERNAL_ERROR_CODES:
            status_code = match.group(1)
            break

    # Extract URL
    url_match = re.search(r"https?://[^\s'\"\)>]+", error_str)
    url = url_match.group(0) if url_match else ""

    # Check for error type indicators
    has_indicator = any(ind in error_str for ind in EXTERNAL_ERROR_INDICATORS)

    return bool(status_code) or has_indicator, status_code, url
