"""Pure helpers for assembling source-link dicts in API responses.

Kept import-light (stdlib plus one constants module) so the logic can be
unit-tested without importing :mod:`api.routers.user.chat`, which pulls in Ray,
the audio loaders, and the OpenAI SDK.
"""

from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote

from core.utils.consts import RETRIEVAL_SCORE_KEYS

# Server-computed and authoritative: never taken from chunk metadata, which a
# write path could have injected them into. Stripped from the nested ``chunk``
# dict as well as the top level, so a client reading ``entry["chunk"]["file_url"]``
# can't be handed the attacker-controlled value the top level already rejected.
_AUTHORITATIVE_KEYS = ("source_type", "chunk_url", "file_url")

# Retrieval-time scores ride in chunk metadata (``ScoredChunk.to_langchain``)
# but are not *of* the chunk — they describe how this query ranked it, and the
# same chunk retrieved by another query gets different ones. So they surface as
# siblings of ``chunk`` rather than inside it.
#
# Promoting them here is only safe because the read boundary
# (``vector_store_searcher._dict_to_chunk``) drops these keys from persisted
# metadata: a score reaching this function was set by *this* retrieval, not
# written by whoever uploaded the file. Both ends share ``RETRIEVAL_SCORE_KEYS``
# so the guard and the promotion can't drift apart.
_SCORE_KEYS = RETRIEVAL_SCORE_KEYS


def build_document_source_link(
    doc_metadata: dict,
    static_url_builder: Callable[[str], str],
    chunk_url_builder: Callable[[str], str],
) -> dict:
    """Build the response dict for one document source.

    The chunk's own metadata is nested under ``chunk`` rather than spread across
    the entry, so what the chunk carries stays visibly separate from what this
    endpoint computed about it (``source_type``, the URLs) and from what this
    retrieval scored it (``rerank_score``). Nesting also makes the spoofing
    guard structural instead of a manual overwrite: a metadata key can no longer
    collide with an authoritative one, because they no longer share a namespace.

    ``file_url`` is emitted only when the chunk has a non-empty ``source``
    filename, so a chunk with no source never produces a download URL. The URL
    is keyed by the chunk id (``_id``): the download is authorized server-side
    by partition membership, so it never exposes a raw, unguarded filesystem
    path. The URL is percent-encoded.

    Args:
        doc_metadata: The chunk metadata (must contain ``_id``).
        static_url_builder: Maps an extract id to its authorized download URL.
        chunk_url_builder: Maps an extract id to its chunk URL.
    """
    source = doc_metadata.get("source") or ""
    filename = Path(source).name

    link = {
        "source_type": "document",
        "chunk": {
            key: value
            for key, value in doc_metadata.items()
            if key not in _AUTHORITATIVE_KEYS and key not in _SCORE_KEYS
        },
    }
    link.update({key: doc_metadata[key] for key in _SCORE_KEYS if key in doc_metadata})
    link["chunk_url"] = chunk_url_builder(doc_metadata["_id"])
    if filename:
        link["file_url"] = quote(static_url_builder(doc_metadata["_id"]), safe=":/")
    return link
