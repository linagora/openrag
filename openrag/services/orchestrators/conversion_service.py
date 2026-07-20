"""ConversionService — document extraction + chunk lookup (Phase 8E).

Business logic extracted from ``routers/tools.py`` (the ``extractText``
tool) and ``routers/extract.py`` (chunk-by-id lookup). Both were thin
wrappers; this service keeps them Ray-free:

- serialization goes through the :class:`~core.indexing.serializer.FileSerializer`
  port (the container injects the in-process ``ParserFileSerializer``, which runs
  the parser dispatcher directly — GPU backends still dispatch to their pools);
- chunk lookup goes through the clean :class:`VectorStore` port
  (``query_chunks_by_filter`` on the Milvus ``_id``), mirroring how
  PartitionService reads chunks — no LangChain ``Document`` leaks out.

The thin routers keep HTTP transport only: file save + cleanup IO, tool
dispatch, the request-scoped partition authorization, and the guards
whose exact ``{"detail": ...}`` body the legacy endpoints returned via
``HTTPException`` (404 not-found, 403 forbidden, the 4xx/5xx tool-error
mapping).

Constructor note: the plan's ``ConversionService(config=config)`` is
underspecified — it takes the two ports it actually needs plus the
established ``collection`` extra (the vector-store collection name the
legacy shim read from ``config.vectordb.collection_name``), supplied by
the container from settings so the service stays Ray/config-free (8H).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.utils.conts import is_internal_metadata_key
from core.utils.logging import get_logger
from core.utils.text import sanitize_extracted_text

if TYPE_CHECKING:
    from core.indexing.serializer import FileSerializer
    from core.vector_stores import VectorStore

logger = get_logger()


class ConversionService:
    """File-to-text extraction and single-chunk retrieval."""

    def __init__(
        self,
        *,
        serializer: FileSerializer,
        vector_store: VectorStore,
        collection: str,
    ) -> None:
        self._serializer = serializer
        self._vector_store = vector_store
        self._collection = collection

    async def serialize_file(
        self,
        *,
        file_path: str,
        filename: str | None,
        metadata: dict,
    ) -> str:
        """Serialize ``file_path`` to sanitized raw text (``extractText``)."""
        metadata = dict(metadata or {})
        metadata.update({"source": str(file_path), "filename": filename})
        content = await self._serializer.serialize(file_path, metadata)
        return sanitize_extracted_text(content)

    async def get_chunk(self, chunk_id: str) -> dict | None:
        """Return ``{"page_content", "metadata"}`` for a chunk, or ``None``.

        Milvus ``_id`` is Int64; a non-integer id is treated as not
        found (the router maps ``None`` to a 404), matching the legacy
        ``get_chunk_by_id``.
        """
        try:
            chunk_id_int = int(chunk_id)
        except (ValueError, TypeError):
            logger.warning("Invalid chunk_id format - must be an integer", chunk_id=chunk_id)
            return None

        rows = await self._vector_store.query_chunks_by_filter(
            self._collection,
            {"_id": chunk_id_int},
        )
        if not rows:
            return None
        row = rows[0]
        return {
            "page_content": row["text"],
            "metadata": _public_chunk_metadata(row),
        }


__all__ = ["ConversionService"]


def _public_chunk_metadata(row: dict) -> dict:
    return {
        key: value for key, value in row.items() if key not in ("text", "vector") and not is_internal_metadata_key(key)
    }
