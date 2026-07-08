"""Framework-free validators for indexing inputs.

Pure functions on plain types — no FastAPI, no Hydra. Routers translate
incoming HTTP requests into these inputs and let the global ``OpenRAGError``
handler convert raised ``ValidationError`` instances into HTTP responses.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

from ..utils.exceptions import ValidationError

DEFAULT_FORBIDDEN_CHARS_IN_FILE_ID: frozenset[str] = frozenset("/")

# Identifiers (file_id, partition name) are interpolated into Milvus filter
# expression strings (e.g. ``file_id == "..."``). Even though the store escapes
# values, restrict identifiers to a safe allowlist as defence-in-depth and input
# hygiene so quotes / brackets / operators can never reach a filter literal.
_VALID_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9._:\-]+")


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
    """Return normalized ``file_id`` if valid, else raise ``ValidationError`` (HTTP 400)."""
    if not isinstance(file_id, str):
        raise ValidationError("File ID must be a string.", status_code=400)
    file_id = file_id.strip()
    if not file_id:
        raise ValidationError("File ID cannot be empty.", status_code=400)
    if _VALID_IDENTIFIER_RE.fullmatch(file_id) is None:
        raise ValidationError(
            "File ID may only contain letters, digits, '.', '_', ':' and '-'.",
            status_code=400,
        )
    if any(char in file_id for char in forbidden_chars):
        raise ValidationError("File ID contains forbidden characters.", status_code=400)
    return file_id


def validate_partition_name(partition: str) -> str:
    """Return ``partition`` if it is a safe identifier, else raise ``ValidationError``.

    Used on partition creation and on every partition-scoped operation so a
    crafted name can never reach a Milvus filter expression string.
    """
    if not isinstance(partition, str) or not partition or _VALID_IDENTIFIER_RE.fullmatch(partition) is None:
        raise ValidationError(
            "Partition name may only contain letters, digits, '.', '_', ':' and '-'.",
            status_code=400,
        )
    return partition


def validate_file_format(
    filename: str | None,
    accepted_formats: Iterable[str],
    accepted_mimetypes: Iterable[str],
    mimetype: str | None = None,
) -> str:
    """Validate the file by extension or mimetype.

    Returns the lowercased file extension (without the leading dot, possibly
    empty). Raises ``ValidationError`` (HTTP 415) on rejection, or
    ``ValidationError`` (HTTP 422) when the filename is missing from the
    multipart upload.
    """
    if not filename:
        raise ValidationError(
            "Uploaded file part has no filename.",
            status_code=422,
        )
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
