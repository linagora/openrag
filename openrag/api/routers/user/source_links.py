"""Pure helpers for assembling source-link dicts in API responses.

Kept import-light (stdlib only) so the logic can be unit-tested without
importing :mod:`api.routers.user.chat`, which pulls in Ray, the audio
loaders, and the OpenAI SDK.
"""

from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote


def build_document_source_link(
    doc_metadata: dict,
    static_url_builder: Callable[[str], str],
    chunk_url_builder: Callable[[str], str],
) -> dict:
    """Build the response dict for one document source.

    ``file_url`` is emitted only when the metadata yields a non-empty
    filename, so a missing/empty ``source`` never produces a static URL with
    an empty path. The static URL is percent-encoded.

    Args:
        doc_metadata: The chunk metadata (must contain ``_id``).
        static_url_builder: Maps a filename to its static file URL.
        chunk_url_builder: Maps an extract id to its chunk URL.
    """
    source = doc_metadata.get("source") or ""
    filename = Path(source).name
    encoded_url = None
    if filename:
        encoded_url = quote(static_url_builder(filename), safe=":/")
    return {
        "source_type": "document",
        **({"file_url": encoded_url} if encoded_url else {}),
        "chunk_url": chunk_url_builder(doc_metadata["_id"]),
        **doc_metadata,
    }
