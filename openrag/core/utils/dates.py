"""Date parsing shared by the code that reads a chunk's temporal metadata."""

from __future__ import annotations

from datetime import UTC, datetime


def parse_datetime(value: object) -> datetime | None:
    """A timezone-aware datetime from an ISO 8601 string or datetime; ``None`` otherwise.

    A naive value is read as UTC, matching the upload-time normalisation in
    ``extract_temporal_fields``.
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
    else:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
