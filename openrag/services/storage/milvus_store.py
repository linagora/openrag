"""Milvus 2.6 vector store adapter implementing :class:`VectorStore`.

Scope:
    Pure vector operations against a single Milvus collection (the one named
    in ``config.vectordb.collection_name``). Embedding, metadata persistence,
    surrounding-chunk hydration, workspace resolution, and cross-store
    orchestration live elsewhere.

Collection model:
    OpenRAG uses **one shared Milvus collection with a partition_key field**.
    The ``collection`` argument on the :class:`VectorStore` ABC therefore maps
    to the **``partition`` row-value** tagged on each entity, not to a Milvus
    collection name. ``ensure_collection`` / ``drop_collection`` operate at
    partition-row granularity.

Client split (Milvus 2.6):
    ``AsyncMilvusClient`` covers the data plane (``insert``, ``search``,
    ``hybrid_search``, ``query``, ``delete``, ``upsert``). The admin/lifecycle
    plane (``has_collection``, ``create_collection``, ``load_collection``,
    ``alter_collection_properties``, ``describe_collection``,
    ``query_iterator``, ``prepare_index_params``) is sync-only, so the sync
    :class:`MilvusClient` is kept alongside.

Hybrid BM25:
    Milvus 2.6 native ``Function(FunctionType.BM25)`` computes the sparse
    vector server-side from the ``text`` field at both insert and query time.
    Hybrid is config-driven, not a separate entry point: :meth:`search`
    dispatches to :meth:`_hybrid_search` when ``config.hybrid_search`` is on
    and :meth:`_dense_search` otherwise. The ``query_text`` argument carries
    the raw query Milvus's server-side BM25 ``Function`` needs alongside the
    dense embedding; the dense-only path ignores it.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from pymilvus import (
    AnnSearchRequest,
    AsyncMilvusClient,
    DataType,
    Function,
    FunctionType,
    MilvusClient,
    MilvusException,
    RRFRanker,
)

from openrag.core.config.infrastructure import VectorDBConfig
from openrag.core.models.chunk import Chunk
from openrag.core.utils.exceptions import (
    UnexpectedVDBError,
    VDBConnectionError,
    VDBCreateOrLoadCollectionError,
    VDBDeleteError,
    VDBInsertError,
    VDBSchemaMigrationRequiredError,
    VDBSearchError,
)
from openrag.core.vector_stores import VectorStore

# ---------------------------------------------------------------------------
# Module constants — lifted verbatim from the legacy MilvusDB so the schema
# is bit-for-bit identical and existing collections load without migration.
# ---------------------------------------------------------------------------

#: Milvus VARCHAR upper bound used for ``text`` / ``partition`` / ``file_id``.
MAX_LENGTH = 65_535

#: Custom collection property holding the schema version integer.
SCHEMA_VERSION_PROPERTY_KEY = "openrag.schema_version"

#: Scalar time fields that get an ``STL_SORT`` index.
INDEXED_TIME_FIELDS = ["created_at"]

#: Dense ANN search params for the HNSW/COSINE index on ``vector``. ``ef``
#: governs the search-time candidate pool size and trades recall for latency.
DEFAULT_DENSE_SEARCH_PARAMS: dict[str, Any] = {
    "metric_type": "COSINE",
    "params": {"ef": 64},
}

#: COSINE upper bound for range search. With ``metric_type="COSINE"`` Milvus
#: keeps hits whose similarity is in ``(radius, range_filter]``; cosine
#: similarity maxes at 1.0, so this is the inclusive ceiling and
#: ``similarity_threshold`` supplies the exclusive ``radius`` floor.
COSINE_RANGE_FILTER_MAX = 1.0

#: BM25 search params for the SPARSE_INVERTED_INDEX on ``sparse``.
#: ``drop_ratio_build`` matches the legacy MilvusDB tuning.
DEFAULT_BM25_SEARCH_PARAMS: dict[str, Any] = {
    "metric_type": "BM25",
    "params": {"drop_ratio_build": 0.2},
}

#: Native Milvus 2.6 RRF fusion constant — k=100 matches the legacy MilvusDB
#: tuning and the rank-fusion literature default.
RRF_K = 100

#: Entity-level keys to strip from search-result records — ``vector`` is
#: noisy and large; ``text`` stays in the payload (callers need it).
_SEARCH_RESULT_DROPPED_KEYS = frozenset({"vector"})

#: Fallback dense-vector dimension for page sizing when the real one is
#: unknown — i.e. a read-only process that never ran ``initialize`` AND the
#: collection-schema probe also failed. Conservatively large so a vector page
#: stays under Milvus's result-size cap for any realistic embedder.
_UNKNOWN_VECTOR_DIM = 4096

#: BM25 analyzer params for the ``text`` field — standard tokenizer plus
#: OpenRAG-specific stop words so chunk-boundary / image-placeholder markers
#: don't pollute lexical scores.
analyzer_params: dict[str, Any] = {
    "tokenizer": "standard",
    "filter": [
        {
            "type": "stop",
            "stop_words": [
                "<image_description>",
                "</image_description>",
                "[Image Placeholder]",
                "_english_",
                "_french_",
                "[CHUNK_START]",
                "[CHUNK_END]",
                "[CONTEXT]",
            ],
        }
    ],
}


class MilvusVectorStore(VectorStore):
    """Milvus 2.6 implementation of :class:`VectorStore`.

    The store is constructed cheaply (no I/O); the collection is materialised
    on the first :meth:`initialize` call. ``initialize`` is idempotent and
    takes the embedding dimension as an argument so the schema does not need
    to import the embedder.
    """

    def __init__(self, config: VectorDBConfig) -> None:
        self._config = config
        self._collection_name = config.collection_name
        self._hybrid = config.hybrid_search
        self._uri = f"http://{config.host}:{config.port}"
        self._timeout = config.timeout
        try:
            self._client = MilvusClient(uri=self._uri, timeout=self._timeout)
            self._async_client = AsyncMilvusClient(uri=self._uri, timeout=self._timeout)
        except MilvusException as e:
            raise VDBConnectionError(
                f"Failed to connect to Milvus: {e!s}",
                db_url=self._uri,
                db_type="Milvus",
            ) from e

        self._embedding_dimension: int | None = None
        self._loaded = False
        self._load_lock = asyncio.Lock()
        # Connection healing: pymilvus 2.6 exposes no documented client-level
        # reconnect knob (no retry/keepalive params on MilvusClient or
        # AsyncMilvusClient — see api-reference v2.6.x). Trust the gRPC
        # channel's internal handling, same as the legacy MilvusDB. If
        # production drops surface a real issue, revisit with evidence
        # rather than racing pymilvus's internal channel state.

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self, embedding_dimension: int) -> None:
        """Materialise the backing Milvus collection.

        Safe to call multiple times. The first caller wins; concurrent callers
        block on the same lock and observe ``_loaded`` set on exit.

        Args:
            embedding_dimension: Dimensionality of the dense vectors that will
                be upserted. Used to size the ``vector`` field in a fresh
                collection. Ignored if the collection already exists.
        """
        if self._loaded:
            return
        async with self._load_lock:
            if self._loaded:
                return
            self._embedding_dimension = embedding_dimension
            await asyncio.to_thread(self._ensure_loaded)
            self._loaded = True

    def _ensure_loaded(self) -> None:
        """Create-if-absent + load the configured collection.

        Synchronous because the Milvus 2.6 admin/lifecycle endpoints
        (``has_collection``, ``create_collection``, ``load_collection``,
        ``alter_collection_properties``, ``describe_collection``) have no
        async equivalents.
        """
        try:
            if self._client.has_collection(self._collection_name):
                self._check_schema_version()
            else:
                schema = self._create_schema()
                index_params = self._create_index()
                try:
                    self._client.create_collection(
                        collection_name=self._collection_name,
                        schema=schema,
                        consistency_level="Strong",
                        index_params=index_params,
                        enable_dynamic_field=True,
                    )
                except MilvusException as e:
                    raise VDBCreateOrLoadCollectionError(
                        f"Failed to create collection `{self._collection_name}`: {e!s}",
                        collection_name=self._collection_name,
                        operation="create_collection",
                    ) from e
                self._store_schema_version()

            try:
                self._client.load_collection(self._collection_name)
            except MilvusException as e:
                raise VDBCreateOrLoadCollectionError(
                    f"Failed to load collection `{self._collection_name}`: {e!s}",
                    collection_name=self._collection_name,
                    operation="load_collection",
                ) from e

        except VDBCreateOrLoadCollectionError:
            raise
        except VDBSchemaMigrationRequiredError:
            raise
        except Exception as e:
            raise UnexpectedVDBError(
                f"Unexpected error preparing collection `{self._collection_name}`: {e!s}",
                collection_name=self._collection_name,
            ) from e

    # ------------------------------------------------------------------
    # Schema / index
    # ------------------------------------------------------------------

    def _create_schema(self):
        """Build the OpenRAG hybrid schema.

        Fields: auto-id ``_id`` (INT64 PK), ``text`` (VARCHAR + analyzer),
        ``partition`` (VARCHAR, partition_key), ``file_id`` (VARCHAR),
        ``vector`` (FLOAT_VECTOR, dim from :meth:`initialize`), one
        TIMESTAMPTZ per field in :data:`INDEXED_TIME_FIELDS`, and — when
        ``hybrid_search`` is on — ``sparse`` (SPARSE_FLOAT_VECTOR) wired to a
        native :class:`Function` of type :data:`FunctionType.BM25` over
        ``text``.
        """
        if self._embedding_dimension is None:
            raise VDBCreateOrLoadCollectionError(
                "embedding_dimension must be set before building the schema; "
                "call MilvusVectorStore.initialize(dim) first.",
                collection_name=self._collection_name,
                operation="create_schema",
            )

        schema = self._client.create_schema(enable_dynamic_field=True)
        schema.add_field(field_name="_id", datatype=DataType.INT64, is_primary=True, auto_id=True)
        schema.add_field(
            field_name="text",
            datatype=DataType.VARCHAR,
            enable_analyzer=True,
            enable_match=True,
            max_length=MAX_LENGTH,
            analyzer_params=analyzer_params,
        )
        schema.add_field(
            field_name="partition",
            datatype=DataType.VARCHAR,
            max_length=MAX_LENGTH,
            is_partition_key=True,
        )
        schema.add_field(
            field_name="file_id",
            datatype=DataType.VARCHAR,
            max_length=MAX_LENGTH,
        )
        schema.add_field(
            field_name="vector",
            datatype=DataType.FLOAT_VECTOR,
            dim=self._embedding_dimension,
        )

        for time_field in INDEXED_TIME_FIELDS:
            schema.add_field(field_name=time_field, datatype=DataType.TIMESTAMPTZ, nullable=True)

        if self._hybrid:
            schema.add_field(
                field_name="sparse",
                datatype=DataType.SPARSE_FLOAT_VECTOR,
                index_type="SPARSE_INVERTED_INDEX",
            )
            schema.add_function(
                Function(
                    name="text_bm25_emb",
                    function_type=FunctionType.BM25,
                    input_field_names=["text"],
                    output_field_names=["sparse"],
                )
            )

        return schema

    def _create_index(self):
        """Build index params: HNSW/COSINE on ``vector``, inverted on scalars,
        STL_SORT on every :data:`INDEXED_TIME_FIELDS` entry, and — only when
        ``hybrid_search`` is enabled — SPARSE_INVERTED_INDEX/BM25 on
        ``sparse`` (k1=1.2, b=0.75) to mirror the schema gating in
        :meth:`_create_schema`.
        """
        index_params = self._client.prepare_index_params()
        index_params.add_index(
            field_name="file_id",
            index_type="INVERTED",
            index_name="file_id_idx",
        )
        index_params.add_index(
            field_name="partition",
            index_type="INVERTED",
            index_name="partition_idx",
        )
        index_params.add_index(
            field_name="vector",
            index_type="HNSW",
            metric_type="COSINE",
            index_params={"M": 128, "efConstruction": 256, "metric_type": "COSINE"},
        )
        if self._hybrid:
            index_params.add_index(
                field_name="sparse",
                index_name="sparse_idx",
                index_type="SPARSE_INVERTED_INDEX",
                index_params={
                    "metric_type": "BM25",
                    "inverted_index_algo": "DAAT_MAXSCORE",
                    "bm25_k1": 1.2,
                    "bm25_b": 0.75,
                },
            )
        for time_field in INDEXED_TIME_FIELDS:
            index_params.add_index(
                field_name=time_field,
                index_type="STL_SORT",
                index_name=f"{time_field}_idx",
            )
        return index_params

    # ------------------------------------------------------------------
    # Schema versioning
    # ------------------------------------------------------------------

    def _store_schema_version(self) -> None:
        """Persist the configured schema version as a Milvus collection property."""
        self._client.alter_collection_properties(
            collection_name=self._collection_name,
            properties={SCHEMA_VERSION_PROPERTY_KEY: str(self._config.schema_version)},
        )

    def _check_schema_version(self) -> None:
        """Compare stored vs. configured schema version; raise on mismatch.

        Missing or unparseable values default to ``0`` so existing
        pre-versioning collections always trigger an explicit migration step
        rather than silently working on a stale schema.
        """
        expected_version = self._config.schema_version
        desc = self._client.describe_collection(self._collection_name)
        raw = desc.get("properties", {}).get(SCHEMA_VERSION_PROPERTY_KEY)
        try:
            stored_version = int(raw) if raw is not None else 0
        except (ValueError, TypeError):
            stored_version = 0

        if stored_version != expected_version:
            raise VDBSchemaMigrationRequiredError(
                f"Collection `{self._collection_name}` is at schema version "
                f"{stored_version} but the application requires version "
                f"{expected_version}. Please perform the migration script.",
                collection_name=self._collection_name,
                stored_version=stored_version,
                expected_version=expected_version,
            )

    # ------------------------------------------------------------------
    # Collection-arg discipline
    # ------------------------------------------------------------------
    #
    # The :class:`VectorStore` ABC carries a ``collection`` argument on most
    # methods; in Milvus terminology a *collection* is the top-level data
    # container (one per store, set by config) while a *partition* is a row
    # tag implemented via ``partition_key``. This store services exactly one
    # Milvus collection, so the ABC's ``collection`` arg either:
    #
    #   * equals ``self._collection_name``    -> accepted, no-op.
    #   * equals the ABC default ``"default"`` -> treated as "use mine".
    #   * anything else                        -> :class:`ValueError`.
    #
    # Partition row-tagging lives exclusively in ``filters['partition']`` (or
    # in ``Chunk.partition`` on the write path).

    _COLLECTION_DEFAULT_SENTINEL = "default"

    def _resolve_collection(self, collection: str) -> str:
        if collection in (self._collection_name, self._COLLECTION_DEFAULT_SENTINEL):
            return self._collection_name
        raise ValueError(
            f"MilvusVectorStore is bound to collection `{self._collection_name}`; "
            f"got `{collection}`. One store services exactly one Milvus collection — "
            "partitions go in filters, not in the `collection` argument."
        )

    # ------------------------------------------------------------------
    # Filter-expression construction
    # ------------------------------------------------------------------

    #: Filter keys with dedicated semantics, pulled out before the generic
    #: ``key == value`` loop runs. ``partition`` is the partition_key row
    #: tag; ``expr`` is a raw-expression escape hatch.
    _SPECIAL_FILTER_KEYS = frozenset({"partition", "expr"})

    #: Partition values that mean "do not filter by partition".
    _PARTITION_WILDCARDS = frozenset({"all"})

    # Whitespace-stripped, lowercased raw expressions that match every row.
    # ``delete_by_filter`` rejects these so callers don't accidentally wipe
    # the collection through ``filters={"expr": "1==1"}`` — explicit drops
    # must go through :meth:`drop_collection`.
    _TAUTOLOGICAL_EXPRS = frozenset({"true", "1==1"})

    @staticmethod
    def _format_value(value: Any) -> str:
        """Render a scalar as a Milvus filter literal.

        Strings are double-quoted with ``\\`` and ``"`` escaped; bools are
        rendered lower-case; ints / floats pass through unquoted.
        """
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return str(value)
        s = str(value).replace("\\", "\\\\").replace('"', '\\"')
        return f'"{s}"'

    def _build_filter_expr(self, filters: dict[str, Any] | None) -> str:
        """Translate a filter dict into a Milvus boolean expression.

        Rules:
            * ``filters['partition']`` builds the partition_key clause.
              Value ``"all"`` (or ``["all"]``) skips the clause. List/tuple
              values become ``partition in [...]``. Mixing a wildcard with
              explicit partitions in the same list raises ``ValueError`` —
              that combination is rejected rather than silently widened to
              every partition.
            * ``filters['expr']`` is appended verbatim as an escape hatch
              for callers that need operators the dict form cannot express.
            * Any other key with a scalar value becomes ``key == <literal>``.
            * Any other key with a list/tuple value becomes ``key in [...]``.
              Empty list/tuple short-circuits to ``"false"`` (matches no row).

        Workspace-id resolution, role checks, and other PG concerns are
        upstream concerns — they resolve to ``file_id`` lists before reaching
        this store.
        """
        filters = dict(filters or {})
        parts: list[str] = []

        partition = filters.pop("partition", None)
        if isinstance(partition, (list, tuple)):
            has_wildcard = any(p in self._PARTITION_WILDCARDS for p in partition)
            if has_wildcard and len(partition) > 1:
                raise ValueError("`partition` cannot mix wildcard with explicit values.")
            if not has_wildcard and partition:
                quoted = ", ".join(self._format_value(p) for p in partition)
                parts.append(f"partition in [{quoted}]")
        elif partition is not None and partition not in self._PARTITION_WILDCARDS:
            parts.append(f"partition == {self._format_value(partition)}")

        raw_expr = filters.pop("expr", None)

        for key, value in filters.items():
            if key in self._SPECIAL_FILTER_KEYS:
                continue  # already handled above
            if isinstance(value, (list, tuple)):
                if not value:
                    return "false"  # empty IN list — match nothing
                quoted = ", ".join(self._format_value(v) for v in value)
                parts.append(f"{key} in [{quoted}]")
            else:
                parts.append(f"{key} == {self._format_value(value)}")

        if raw_expr:
            parts.append(str(raw_expr))

        if len(parts) <= 1:
            return parts[0] if parts else ""
        # Multiple predicates: wrap each in parentheses when joining so a
        # user-supplied filter (the ``expr`` escape hatch) cannot escape the
        # partition scope via operator precedence — Milvus binds ``and`` tighter
        # than ``or``, so an unparenthesised
        # ``partition == 'p' and text != "" or partition == 'q'`` would leak
        # another tenant's chunks.
        return " and ".join(f"({part})" for part in parts)

    # ------------------------------------------------------------------
    # Sync paginated query helper (Milvus 2.6 query_iterator is sync-only)
    # ------------------------------------------------------------------

    def _vector_dim(self) -> int:
        """ dense-vector dimension for page sizing.

        Prefers the value recorded at :meth:`initialize`. A read-only process
        never indexes, so that may be ``None``; fall back to the live collection
        schema (cached on success) and finally to :data:`_UNKNOWN_VECTOR_DIM` so
        the page stays safe even when the schema can't be read. Reading too
        small a dimension here would re-introduce the oversized-page failure
        this guard exists to prevent.
        """
        if self._embedding_dimension is not None:
            return self._embedding_dimension
        dim = self._describe_vector_dim()
        if dim is not None:
            self._embedding_dimension = dim
            return dim
        return _UNKNOWN_VECTOR_DIM

    def _describe_vector_dim(self) -> int | None:
        """Read the ``vector`` field's ``dim`` from the live collection schema.

        Returns ``None`` if the collection or field can't be inspected (e.g. the
        collection does not exist yet), leaving the caller to pick a fallback.
        """
        try:
            desc = self._client.describe_collection(self._collection_name)
            for field in desc.get("fields", []):
                if field.get("name") == "vector":
                    dim = field.get("params", {}).get("dim")
                    return int(dim) if dim else None
        except Exception:
            return None
        return None

    def _safe_batch_size(self, output_fields: list[str]) -> int:
        """Cap the batch so one page stays under Milvus's ~64MB result limit.

        Only matters when the dense ``vector`` rides along (~dim*4 bytes/row);
        explicit scalar projections are small, so they keep the large default.
        Milvus 2.6 returns the vector for the ``"*"`` wildcard too — the search
        path strips it post-hoc via ``_SEARCH_RESULT_DROPPED_KEYS`` and
        ``query_chunks_by_filter(["*"])`` leaks it — so ``"*"`` counts as
        vector-inclusive here. The dimension comes from :meth:`_vector_dim`, not
        a fixed guess, so a read-only process still sizes pages to the real
        collection.
        """
        if "vector" not in output_fields and "*" not in output_fields:
            return 16_000
        dim = self._vector_dim()
        budget = 32 * 1024 * 1024  # ~half of Milvus's ~64MB cap
        per_row = dim * 4 + 6_144  # float32 vector + text/metadata headroom
        return max(1, min(16_000, budget // per_row))

    def _iter_query(
        self,
        expr: str,
        output_fields: list[str],
        batch_size: int | None = None,
    ) -> list[dict[str, Any]]:
        """Drain a Milvus 2.6 ``query_iterator`` into a list.

        ``batch_size`` defaults to :meth:`_safe_batch_size`, which shrinks the
        page when the dense vector is requested so one page stays under Milvus's
        result-size limit. Total result-set size is unaffected — the iterator
        paginates until the filter is drained.

        Synchronous; call via :func:`asyncio.to_thread` from async methods.
        """
        if batch_size is None:
            batch_size = self._safe_batch_size(output_fields)
        iterator = self._client.query_iterator(
            collection_name=self._collection_name,
            filter=expr,
            batch_size=batch_size,
            output_fields=output_fields,
        )
        out: list[dict[str, Any]] = []
        try:
            while True:
                batch = iterator.next()
                if not batch:
                    break
                out.extend(batch)
        finally:
            iterator.close()
        return out

    # ------------------------------------------------------------------
    # ID round-trip (Chunk.id: str  <-->  Milvus _id: INT64 auto_id PK)
    # ------------------------------------------------------------------

    @staticmethod
    def _str_id_to_milvus(id_str: str) -> int | None:
        """Coerce a ``Chunk.id`` string to a Milvus INT64 ``_id``.

        Returns ``None`` for non-numeric IDs (e.g. fresh UUIDs that have not
        been round-tripped through Milvus yet) so callers can skip them
        rather than crash a batch delete.
        """
        try:
            return int(id_str)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _milvus_id_to_str(id_int: int) -> str:
        """Convert a Milvus ``_id`` (INT64) back to the domain string form."""
        return str(id_int)

    # ------------------------------------------------------------------
    # Entity construction
    # ------------------------------------------------------------------

    @staticmethod
    def _gen_chunk_order_metadata(n: int) -> list[dict[str, int | None]]:
        """Generate prev/section/next IDs for a batch of ``n`` chunks.

        Uses a randomised base so IDs are unique across rapid batches.
        Preserves the legacy MilvusDB ordering so existing
        surrounding-chunk hydration keeps working.
        """
        if n <= 0:
            return []
        int64_max = 2**63 - 1
        random_offset = secrets.randbits(32)
        base = (time.time_ns() + random_offset) % (int64_max - n)
        ids = [base + i for i in range(n)]
        return [
            {
                "prev_section_id": ids[i - 1] if i > 0 else None,
                "section_id": ids[i],
                "next_section_id": ids[i + 1] if i < n - 1 else None,
            }
            for i in range(n)
        ]

    @staticmethod
    def _chunk_to_entity(
        chunk: Chunk,
        *,
        indexed_at: str,
        order: dict[str, int | None],
    ) -> dict[str, Any]:
        """Build the Milvus insert payload for one chunk.

        Layering: start from the free-form ``chunk.metadata`` dict, then
        overwrite with the typed Chunk fields so the strict domain model
        always wins over caller-supplied metadata keys with the same name.
        ``_id`` is intentionally omitted — Milvus assigns it via ``auto_id``.
        """
        entity: dict[str, Any] = dict(chunk.metadata)
        entity.update(
            {
                "text": chunk.text,
                "vector": chunk.embedding,
                "partition": chunk.partition,
                "file_id": chunk.document_id,
                "chunk_type": chunk.chunk_type.value,
                "page": chunk.page_number,
                "indexed_at": indexed_at,
                **order,
            }
        )
        # Optional typed fields only emitted when set, to avoid stamping
        # nulls into the dynamic schema.
        for field, value in (
            ("chunk_index", chunk.chunk_index),
            ("token_count", chunk.token_count),
            ("header", chunk.header),
            ("context", chunk.context),
            ("content", chunk.content),
        ):
            if value is not None:
                entity[field] = value
        return entity

    # ------------------------------------------------------------------
    # VectorStore ABC — writes
    # ------------------------------------------------------------------

    async def upsert(
        self,
        chunks: list[Chunk],
        collection: str = "default",
        *,
        indexed_at: datetime | None = None,
    ) -> int:
        """Insert pre-embedded chunks into the backing Milvus collection.

        ``chunk.partition`` is authoritative — the ``collection`` argument is
        accepted for ABC compatibility but does not override per-chunk
        partition values. Every chunk MUST carry a populated ``embedding``;
        embedding is an upstream pipeline concern, not a store concern.

        ``indexed_at`` lets the caller pin a single indexation timestamp so the
        Milvus chunks and the Postgres ``files`` row agree; when omitted it
        defaults to the current time (legacy behaviour).
        """
        self._resolve_collection(collection)
        if not chunks:
            return 0

        missing = [c.id for c in chunks if c.embedding is None]
        if missing:
            raise VDBInsertError(
                f"upsert received {len(missing)} chunk(s) with no embedding; embed before calling the vector store.",
                collection_name=self._collection_name,
            )

        indexed_at = (indexed_at or datetime.now(UTC)).isoformat()
        order_metadata = self._gen_chunk_order_metadata(len(chunks))
        entities = [
            self._chunk_to_entity(c, indexed_at=indexed_at, order=o)
            for c, o in zip(chunks, order_metadata, strict=True)
        ]

        try:
            result = await self._async_client.insert(
                collection_name=self._collection_name,
                data=entities,
            )
        except MilvusException as e:
            raise VDBInsertError(
                f"Milvus insert failed: {e!s}",
                collection_name=self._collection_name,
            ) from e
        except Exception as e:
            raise UnexpectedVDBError(
                f"Unexpected error during Milvus insert: {e!s}",
                collection_name=self._collection_name,
            ) from e

        # Milvus 2.6 returns {"insert_count": N, "ids": [...], "cost": ...}.
        # Fall back to len(entities) if the server omits insert_count.
        return int(result.get("insert_count", len(entities))) if isinstance(result, dict) else len(entities)

    def _parse_search_response(self, response: Any) -> list[dict[str, Any]]:
        """Normalise a Milvus 2.6 search/hybrid_search response to raw dicts.

        Each record has ``id`` (stringified for :class:`Chunk` round-trip),
        ``score`` (distance for dense, fused RRF score for hybrid), and the
        entity's output fields except ``vector``.
        """
        if not response:
            return []
        out: list[dict[str, Any]] = []
        for hit in response[0]:
            entity = hit.get("entity", {}) if isinstance(hit, dict) else {}
            record = {k: v for k, v in entity.items() if k not in _SEARCH_RESULT_DROPPED_KEYS}
            # The primary key field is named ``_id`` (auto_id INT64), so Milvus
            # exposes it on the hit as ``_id`` and also inside ``entity`` — NOT
            # under the generic ``id`` key. Reading ``id`` yielded a literal
            # "None" string for every search result.
            pk = hit.get("_id")
            if pk is None:
                pk = entity.get("_id")
            record["id"] = self._milvus_id_to_str(pk) if pk is not None else None
            record["score"] = hit.get("distance")
            out.append(record)
        return out

    @contextmanager
    def _search_errors(self, kind: str) -> Iterator[None]:
        """Map Milvus failures from a search call to the VDB error taxonomy.

        Wraps the ``await`` site so :meth:`search` and :meth:`hybrid_search`
        don't each repeat the same two-arm ``MilvusException`` /
        ``Exception`` translation. ``kind`` names the operation for the
        message (``"dense search"`` / ``"hybrid search"``).
        """
        try:
            yield
        except MilvusException as e:
            raise VDBSearchError(
                f"Milvus {kind} failed: {e!s}",
                collection_name=self._collection_name,
            ) from e
        except Exception as e:
            raise UnexpectedVDBError(
                f"Unexpected error during Milvus {kind}: {e!s}",
                collection_name=self._collection_name,
            ) from e

    def _dense_search_params(self, similarity_threshold: float | None) -> dict[str, Any]:
        """Build the dense COSINE search params, optionally range-filtered.

        Returns a fresh dict each call so the frozen
        :data:`DEFAULT_DENSE_SEARCH_PARAMS` module default is never mutated.
        When ``similarity_threshold`` is set, Milvus range search keeps only
        hits whose COSINE similarity falls in
        ``(similarity_threshold, COSINE_RANGE_FILTER_MAX]`` — the same
        ``radius`` / ``range_filter`` pair the legacy MilvusDB used. ``None``
        leaves it an unbounded top-k search.
        """
        params = dict(DEFAULT_DENSE_SEARCH_PARAMS["params"])
        if similarity_threshold is not None:
            params["radius"] = similarity_threshold
            params["range_filter"] = COSINE_RANGE_FILTER_MAX
        return {"metric_type": DEFAULT_DENSE_SEARCH_PARAMS["metric_type"], "params": params}

    async def search(
        self,
        embedding: list[float],
        query_text: str | None = None,
        top_k: int = 10,
        collection: str = "default",
        filters: dict[str, Any] | None = None,
        similarity_threshold: float | None = None,
    ) -> list[dict[str, Any]]:
        """Similarity search — single entry point for dense and hybrid.

        Hybrid is a collection-build property, not a caller choice: the
        store dispatches to :meth:`_hybrid_search` when ``config.hybrid_search``
        was on (the backing collection then has a ``sparse`` BM25 field) and
        to :meth:`_dense_search` otherwise. ``query_text`` is only consumed on
        the hybrid path — Milvus's server-side BM25 ``Function`` generates the
        sparse vector from it; the dense path ignores it.

        Returns raw dicts (``id``, ``score``, plus entity fields except
        ``vector``). ``similarity_threshold`` (when set) is the range-search
        ``radius`` floor on the dense leg; see :meth:`_dense_search_params`.
        """
        if self._hybrid:
            return await self._hybrid_search(embedding, query_text, top_k, collection, filters, similarity_threshold)
        return await self._dense_search(embedding, top_k, collection, filters, similarity_threshold)

    async def _dense_search(
        self,
        embedding: list[float],
        top_k: int,
        collection: str,
        filters: dict[str, Any] | None,
        similarity_threshold: float | None,
    ) -> list[dict[str, Any]]:
        """Dense ANN search on the ``vector`` field.

        Uses HNSW with COSINE distance and ``ef=64`` — same tuning as the
        legacy MilvusDB.
        """
        self._resolve_collection(collection)
        expr = self._build_filter_expr(filters)

        with self._search_errors("dense search"):
            response = await self._async_client.search(
                collection_name=self._collection_name,
                data=[embedding],
                anns_field="vector",
                search_params=self._dense_search_params(similarity_threshold),
                limit=top_k,
                filter=expr,
                output_fields=["*"],
            )

        return self._parse_search_response(response)

    async def _hybrid_search(
        self,
        embedding: list[float],
        query_text: str | None,
        top_k: int,
        collection: str,
        filters: dict[str, Any] | None,
        similarity_threshold: float | None,
    ) -> list[dict[str, Any]]:
        """Dense + Milvus-native BM25 sparse, fused via ``RRFRanker``.

        Only reached when the backing collection was built with
        ``hybrid_search=True`` (it then has the ``sparse`` field). The
        ``query_text`` is required here — Milvus's server-side
        ``Function(FunctionType.BM25)`` generates the sparse vector from it,
        so a missing query would silently drop the lexical signal.

        ``similarity_threshold`` (when set) range-filters the dense leg only;
        the BM25 leg has no comparable distance metric, matching the legacy
        MilvusDB behaviour.

        Raises:
            VDBSearchError: ``query_text`` is ``None`` — the BM25 leg has no
                input.
        """
        self._resolve_collection(collection)
        if query_text is None:
            raise VDBSearchError(
                f"hybrid search on collection `{self._collection_name}` requires "
                "query_text for the server-side BM25 leg; got None.",
                collection_name=self._collection_name,
            )
        expr = self._build_filter_expr(filters)

        dense_req = AnnSearchRequest(
            data=[embedding],
            anns_field="vector",
            param=self._dense_search_params(similarity_threshold),
            limit=top_k,
            expr=expr,
        )
        sparse_req = AnnSearchRequest(
            data=[query_text],
            anns_field="sparse",
            param=DEFAULT_BM25_SEARCH_PARAMS,
            limit=top_k,
            expr=expr,
        )

        with self._search_errors("hybrid search"):
            response = await self._async_client.hybrid_search(
                collection_name=self._collection_name,
                reqs=[dense_req, sparse_req],
                ranker=RRFRanker(RRF_K),
                limit=top_k,
                output_fields=["*"],
            )

        return self._parse_search_response(response)

    async def delete(self, ids: list[str], collection: str = "default") -> int:
        """Delete chunks by Milvus ``_id``.

        ``Chunk.id`` is a string while the Milvus primary key is INT64.
        Non-numeric IDs are silently dropped (they cannot exist in Milvus by
        construction) so a partially-fresh batch doesn't fail the whole call.
        The ``collection`` argument is accepted for ABC compatibility; the
        Milvus delete is scoped to the backing collection regardless, and
        rows are uniquely keyed by ``_id``.
        """
        self._resolve_collection(collection)
        if not ids:
            return 0

        numeric_ids = [n for n in (self._str_id_to_milvus(i) for i in ids) if n is not None]
        if not numeric_ids:
            return 0

        try:
            result = await self._async_client.delete(
                collection_name=self._collection_name,
                ids=numeric_ids,
            )
        except MilvusException as e:
            raise VDBDeleteError(
                f"Milvus delete failed: {e!s}",
                collection_name=self._collection_name,
            ) from e
        except Exception as e:
            raise UnexpectedVDBError(
                f"Unexpected error during Milvus delete: {e!s}",
                collection_name=self._collection_name,
            ) from e

        return int(result.get("delete_count", 0)) if isinstance(result, dict) else 0

    async def upsert_entities(self, entities: list[dict[str, Any]], collection: str = "default") -> int:
        """Upsert raw Milvus entities that already include vector data.

        This is intentionally narrower than the VectorStore port: it supports
        file metadata patch/copy paths where re-embedding would be wrong and
        the existing Milvus rows already carry the vectors to preserve.
        """
        self._resolve_collection(collection)
        if not entities:
            return 0

        try:
            result = await self._async_client.upsert(
                collection_name=self._collection_name,
                data=entities,
            )
        except MilvusException as e:
            raise VDBInsertError(
                f"Milvus raw entity upsert failed: {e!s}",
                collection_name=self._collection_name,
            ) from e
        except Exception as e:
            raise UnexpectedVDBError(
                f"Unexpected error during Milvus raw entity upsert: {e!s}",
                collection_name=self._collection_name,
            ) from e

        return int(result.get("upsert_count", len(entities))) if isinstance(result, dict) else len(entities)

    async def insert_entities(self, entities: list[dict[str, Any]], collection: str = "default") -> int:
        """Insert raw Milvus entities that already include vector data."""
        self._resolve_collection(collection)
        if not entities:
            return 0

        try:
            result = await self._async_client.insert(
                collection_name=self._collection_name,
                data=entities,
            )
        except MilvusException as e:
            raise VDBInsertError(
                f"Milvus raw entity insert failed: {e!s}",
                collection_name=self._collection_name,
            ) from e
        except Exception as e:
            raise UnexpectedVDBError(
                f"Unexpected error during Milvus raw entity insert: {e!s}",
                collection_name=self._collection_name,
            ) from e

        return int(result.get("insert_count", len(entities))) if isinstance(result, dict) else len(entities)

    async def ensure_collection(self, name: str, dimension: int, **kwargs: Any) -> None:
        """Public entry point for materialising the backing collection.

        Thin wrapper over :meth:`initialize`: validates ``name`` against the
        bound collection (so a future per-tenant store factory cannot
        accidentally cross-wire one tenant's collection name into another's
        store) and forwards ``dimension``. Idempotent.

        Raises:
            ValueError: ``name`` is neither ``self._collection_name`` nor
                the ABC sentinel ``"default"``.
            ValueError: the store is already initialized with a different
                embedding dimension — re-initialising would invalidate the
                index, so callers must drop and re-create explicitly.
        """
        self._resolve_collection(name)
        if self._loaded and self._embedding_dimension != dimension:
            raise ValueError(
                f"MilvusVectorStore already initialised at "
                f"dimension={self._embedding_dimension}; "
                f"got ensure_collection(dimension={dimension}). "
                "Drop the collection before re-sizing."
            )
        await self.initialize(dimension)

    async def drop_collection(self, name: str) -> None:
        """Destructive: drop the entire backing Milvus collection.

        For administrative / test fixture use only. To remove rows for a
        specific partition or any filterable subset, call
        :meth:`delete_by_filter` instead — that is the surface the 7C shim
        uses for partition-level deletion.
        """
        self._resolve_collection(name)
        try:
            await asyncio.to_thread(self._client.drop_collection, self._collection_name)
        except MilvusException as e:
            raise VDBDeleteError(
                f"Failed to drop collection `{self._collection_name}`: {e!s}",
                collection_name=self._collection_name,
            ) from e
        except Exception as e:
            raise UnexpectedVDBError(
                f"Unexpected error dropping collection `{self._collection_name}`: {e!s}",
                collection_name=self._collection_name,
            ) from e
        self._loaded = False
        self._embedding_dimension = None

    # ------------------------------------------------------------------
    # Milvus-specific (not on the VectorStore ABC)
    # ------------------------------------------------------------------

    async def delete_by_filter(self, filters: dict[str, Any]) -> int:
        """Delete every row whose entity matches the filter expression.

        Used by callers that want to remove a partition's worth of rows
        without first paginating all chunk IDs (e.g. the legacy
        ``delete_partition`` flow). Guarded so an accidental empty /
        wildcard filter does NOT nuke the entire collection — explicit
        drop is :meth:`drop_collection`.

        Raises:
            ValueError: ``filters`` builds an empty Milvus expression
                (no clauses, or only a wildcard partition), or resolves to
                a tautological raw expression such as ``"1==1"``/``"true"``
                that would wipe the collection.
        """
        expr = self._build_filter_expr(filters)
        normalized = "".join(expr.lower().split()) if expr else ""
        if not expr or normalized in self._TAUTOLOGICAL_EXPRS:
            raise ValueError(
                "delete_by_filter requires a non-empty, non-tautological "
                "filter expression. To delete every row, call "
                "drop_collection() explicitly."
            )
        try:
            result = await self._async_client.delete(
                collection_name=self._collection_name,
                filter=expr,
            )
        except MilvusException as e:
            raise VDBDeleteError(
                f"Milvus delete-by-filter failed (expr=`{expr}`): {e!s}",
                collection_name=self._collection_name,
            ) from e
        except Exception as e:
            raise UnexpectedVDBError(
                f"Unexpected error during Milvus delete-by-filter: {e!s}",
                collection_name=self._collection_name,
            ) from e

        return int(result.get("delete_count", 0)) if isinstance(result, dict) else 0

    async def collection_exists(self, name: str) -> bool:
        """Report whether the Milvus collection exists on the server.

        Accepts ``self._collection_name`` or the ABC default ``"default"``;
        any other name falsifies (we don't query other collections — this
        store services exactly one).
        """
        if name not in (self._collection_name, self._COLLECTION_DEFAULT_SENTINEL):
            return False
        return await asyncio.to_thread(self._client.has_collection, self._collection_name)

    async def query_ids_by_filter(
        self,
        collection: str,
        filters: dict[str, Any],
    ) -> list[str]:
        """Return ``Chunk.id`` strings for every row matching ``filters``.

        Uses Milvus 2.6 ``query_iterator`` under the hood so result-set size
        is bounded only by Milvus pagination, not by a server-side
        ``limit``. The returned IDs are the INT64 ``_id`` values stringified
        for round-trip with :class:`Chunk`.
        """
        self._resolve_collection(collection)
        expr = self._build_filter_expr(filters)
        rows = await asyncio.to_thread(self._iter_query, expr, ["_id"])
        return [self._milvus_id_to_str(r["_id"]) for r in rows if "_id" in r]

    async def query_chunks_by_filter(
        self,
        collection: str,
        filters: dict[str, Any],
        output_fields: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return full row data for every chunk matching ``filters``.

        ``output_fields`` defaults to ``["*"]``. Milvus 2.6 quirk: ``"*"``
        does NOT include the dense vector — callers that need the vector
        must pass ``output_fields=["*", "vector"]`` explicitly.
        """
        self._resolve_collection(collection)
        expr = self._build_filter_expr(filters)
        fields = output_fields or ["*"]
        return await asyncio.to_thread(self._iter_query, expr, fields)
