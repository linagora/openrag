PARTITION_PREFIX = "openrag-"
LEGACY_PARTITION_PREFIX = "ragondin-"

FILE_READ_CHUNK_SIZE = 1024 * 1024  # Read file in blocks of 1MB to preserve RAM


IMG_WRAPPER_OPEN = "<image_description>\n\n"
IMG_WRAPPER_CLOSE = "\n\n</image_description>"

IMAGE_PLACEHOLDER = f"""{IMG_WRAPPER_OPEN}[Image Placeholder]{IMG_WRAPPER_CLOSE}"""

INTERNAL_METADATA_PREFIX = "_openrag"


def is_internal_metadata_key(key: object) -> bool:
    return isinstance(key, str) and key.startswith(INTERNAL_METADATA_PREFIX)


def strip_internal_metadata(row: dict) -> dict:
    return {key: value for key, value in row.items() if not is_internal_metadata_key(key)}


# Catalog/store-managed fields a caller must never be able to spoof through the
# free-form metadata dict on a write path. ``source`` is the load-bearing one:
# it is the filesystem path served by ``GET /static/{extract_id}``, so letting a
# caller set it turns a metadata write into a cross-tenant file read (#713).
#
# ``content_sha256`` is the server-computed dedup hash: a caller-set value would
# corrupt dedup (spoofed ``DOCUMENT_CONTENT_EXISTS``), so it is protected here and
# re-set from the server side on the copy path after this strip runs.
#
# ``partition`` is intentionally NOT here — the MCP update tool uses it as an
# authorized move control and re-checks editor access on the destination.
#
# Lives in core/ (not in one orchestrator) so every transport shares one guard:
# the upload path, the MCP tools, and the REST PATCH path previously each
# re-implemented this and the REST one simply omitted it.
PROTECTED_METADATA_KEYS: frozenset[str] = frozenset(
    {"file_id", "source", "created_by", "file_size", "file_count", "_id", "vector", "text", "content_sha256"}
)


def strip_protected_metadata(metadata: dict | None) -> tuple[dict, list[str]]:
    """Drop server-managed keys from caller-supplied metadata.

    Returns the cleaned dict and the sorted list of dropped keys so the caller
    can log the rejection with its own context. Never mutates the input.
    """
    md = dict(metadata or {})
    removed = sorted(k for k in md if k in PROTECTED_METADATA_KEYS)
    for key in removed:
        del md[key]
    return md, removed


# The exact keys ``IndexingService._build_metadata`` computes and merges into
# the caller-supplied upload metadata (source path, sanitized/original
# filename, human-readable size, file_id, content hash). Distinct from
# ``PROTECTED_METADATA_KEYS`` above: that one guards writes (strip these if a
# caller tries to set them); this one guards a read — the indexing-status
# callback echoes the merged dict back to a caller-supplied URL and must
# exclude exactly these, not the caller's own fields, which the caller learns
# nothing new by getting back. Keep this in sync with ``_build_metadata`` —
# ``test_build_metadata_only_adds_keys_in_upload_metadata_server_keys`` fails
# the build if the two ever drift apart.
UPLOAD_METADATA_SERVER_KEYS: frozenset[str] = frozenset(
    {"source", "filename", "original_filename", "file_size", "file_id", "content_sha256"}
)
