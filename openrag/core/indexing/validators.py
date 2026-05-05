"""Framework-free validators for indexing inputs.

Pure functions on plain types — no FastAPI, no Hydra. Routers translate
incoming HTTP requests into these inputs and let the global ``OpenRAGError``
handler convert raised ``ValidationError`` instances into HTTP responses.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from ..utils.exceptions import ValidationError

DEFAULT_FORBIDDEN_CHARS_IN_FILE_ID: frozenset[str] = frozenset("/")


def parse_metadata(raw: Any | None) -> dict:
    """Parse JSON-encoded metadata into a dict.

    Accepts ``None`` / empty string (returns ``{}``), an existing dict
    (returned as-is), or a JSON string that decodes to a dict.
    """
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValidationError("Invalid JSON in metadata", status_code=400) from exc
    if not isinstance(decoded, dict):
        raise ValidationError("Metadata must be a JSON object", status_code=400)
    return decoded


def validate_file_id(
    file_id: str,
    forbidden_chars: Iterable[str] = DEFAULT_FORBIDDEN_CHARS_IN_FILE_ID,
) -> str:
    """Return ``file_id`` if valid, else raise ``ValidationError`` (HTTP 400)."""
    forbidden = frozenset(forbidden_chars)
    if any(c in file_id for c in forbidden):
        raise ValidationError(
            f"File ID contains forbidden characters: {', '.join(sorted(forbidden))}",
            status_code=400,
        )
    if not file_id.strip():
        raise ValidationError("File ID cannot be empty.", status_code=400)
    return file_id


def validate_file_format(
    filename: str,
    accepted_formats: Iterable[str],
    accepted_mimetypes: Iterable[str],
    mimetype: str | None = None,
) -> str:
    """Validate the file by extension or mimetype.

    Returns the lowercased file extension (without the leading dot, possibly
    empty). Raises ``ValidationError`` (HTTP 415) on rejection.
    """
    file_extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    formats = set(accepted_formats)
    mimetypes = set(accepted_mimetypes)
    if file_extension not in formats and mimetype not in mimetypes:
        details = (
            f"Unsupported file format: {file_extension} or file mimetype.\n"
            f"Supported formats: {', '.join(sorted(formats))}\n"
            f"Supported mimetypes: {', '.join(sorted(mimetypes))}"
        )
        raise ValidationError(details, status_code=415)
    return file_extension
