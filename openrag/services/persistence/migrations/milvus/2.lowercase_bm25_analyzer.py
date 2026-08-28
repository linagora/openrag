"""
Milvus migration: case-insensitive BM25 analyzer  (schema version 1 → 2)
========================================================================
Adds the ``lowercase`` filter to the ``text`` analyzer so BM25 stops treating
``Rapport`` and ``rapport`` as distinct terms.

Rebuilds the collection rather than altering it: Milvus 3.0 refuses to alter
``analyzer_params`` while ``enable_match`` is set (and ``enable_match`` is itself
immutable), and ``add_function_field`` — which would re-attach BM25 with a
backfill — requires Storage V3, off by default. Both were probed against 3.0.0.

Copies rows into ``<collection>_v2_rebuild``, verifies the counts, then swaps
names, keeping the old collection as ``<collection>_v1_backup`` (never dropped
automatically). Dense vectors are copied verbatim and the sparse vectors are
regenerated server-side on insert, so nothing is re-embedded. Collections with
no ``sparse`` field are only re-stamped.

``_id`` is ``auto_id``, so the copy assigns new chunk IDs. Postgres keys on
``file_id``, but a chunk ID handed out earlier by MCP no longer resolves.

Usage — prefer the generic runner (from repo root, inside the container):
    docker compose run --no-deps --rm --entrypoint "" openrag \\
        uv run python services/persistence/migrations/milvus/migrate.py [--dry-run]

This script also runs standalone with ``--dry-run`` / ``--downgrade``.
"""

import argparse
import json
import sys
from datetime import datetime
from typing import Any

from core.config import load_config
from core.utils.logging import get_logger
from pymilvus import DataType, Function, FunctionType, MilvusClient
from services.storage.milvus_store import SCHEMA_VERSION_PROPERTY_KEY

TARGET_VERSION = 2  # The schema version this migration brings the collection to.

# Frozen snapshot of the v2 schema — importing the live one would let a later
# analyzer change rewrite what this migration produces.
ANALYZER_PARAMS_V2: dict[str, Any] = {
    "tokenizer": "standard",
    "filter": [
        "lowercase",
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
        },
    ],
}

MAX_LENGTH = 65_535
INDEXED_TIME_FIELDS = ["created_at"]

REBUILD_SUFFIX = "_v2_rebuild"
BACKUP_SUFFIX = "_v1_backup"
ROLLBACK_ASIDE_SUFFIX = "_v2_rolled_back"

#: Upper bound on rows per copy batch, further capped by the vector payload.
MAX_COPY_BATCH = 1_000

#: Keys that must never be written back on insert: ``_id`` is an ``auto_id``
#: primary key and ``sparse`` is a BM25 ``Function`` output — Milvus rejects
#: caller-supplied values for both.
_UNSETTABLE_KEYS = frozenset({"_id", "sparse"})

logger = get_logger()


# ---------------------------------------------------------------------------
# Introspection helpers
# ---------------------------------------------------------------------------


def _get_stored_version(client: MilvusClient, collection_name: str) -> int:
    desc = client.describe_collection(collection_name)
    raw = desc.get("properties", {}).get(SCHEMA_VERSION_PROPERTY_KEY)
    if raw is None:
        return 0
    try:
        return int(raw)
    except ValueError:
        return 0


def _field_map(desc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {f["name"]: f for f in desc.get("fields", [])}


def _is_hybrid(desc: dict[str, Any]) -> bool:
    """True when the collection carries the BM25 sparse field."""
    return "sparse" in _field_map(desc)


def _vector_dim(desc: dict[str, Any]) -> int:
    field = _field_map(desc).get("vector")
    if field is None:
        raise RuntimeError("Collection has no `vector` field — not an OpenRAG collection.")
    dim = field.get("params", {}).get("dim")
    if not dim:
        raise RuntimeError("Could not read the `vector` field dimension from the collection schema.")
    return int(dim)


def _max_length(desc: dict[str, Any], field_name: str) -> int:
    field = _field_map(desc).get(field_name, {})
    return int(field.get("params", {}).get("max_length", MAX_LENGTH))


def _analyzer_has_lowercase(desc: dict[str, Any]) -> bool:
    """True when the live ``text`` analyzer already applies the lowercase filter."""
    raw = _field_map(desc).get("text", {}).get("params", {}).get("analyzer_params")
    if not raw:
        return False
    try:
        params = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return False
    return any(f == "lowercase" for f in params.get("filter", []) if isinstance(f, str))


def _row_count(client: MilvusClient, collection_name: str) -> int:
    client.load_collection(collection_name)
    rows = client.query(collection_name=collection_name, filter="", output_fields=["count(*)"])
    return int(rows[0]["count(*)"]) if rows else 0


# ---------------------------------------------------------------------------
# Schema construction (v2)
# ---------------------------------------------------------------------------


def _build_v2_schema(client: MilvusClient, desc: dict[str, Any]):
    """Rebuild the OpenRAG schema with the corrected analyzer.

    Per-deployment values (vector dimension, VARCHAR lengths, whether the
    collection is hybrid) are read from the live collection rather than
    assumed, so a collection created with non-default settings round-trips.
    """
    schema = client.create_schema(enable_dynamic_field=True)
    schema.add_field(field_name="_id", datatype=DataType.INT64, is_primary=True, auto_id=True)
    schema.add_field(
        field_name="text",
        datatype=DataType.VARCHAR,
        enable_analyzer=True,
        enable_match=True,
        max_length=_max_length(desc, "text"),
        analyzer_params=ANALYZER_PARAMS_V2,
    )
    schema.add_field(
        field_name="partition",
        datatype=DataType.VARCHAR,
        max_length=_max_length(desc, "partition"),
        is_partition_key=True,
    )
    schema.add_field(
        field_name="file_id",
        datatype=DataType.VARCHAR,
        max_length=_max_length(desc, "file_id"),
    )
    schema.add_field(field_name="vector", datatype=DataType.FLOAT_VECTOR, dim=_vector_dim(desc))

    for time_field in INDEXED_TIME_FIELDS:
        schema.add_field(field_name=time_field, datatype=DataType.TIMESTAMPTZ, nullable=True)

    if _is_hybrid(desc):
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


def _build_v2_index_params(client: MilvusClient, desc: dict[str, Any]):
    index_params = client.prepare_index_params()
    index_params.add_index(field_name="file_id", index_type="INVERTED", index_name="file_id_idx")
    index_params.add_index(field_name="partition", index_type="INVERTED", index_name="partition_idx")
    index_params.add_index(
        field_name="vector",
        index_type="HNSW",
        metric_type="COSINE",
        index_params={"M": 128, "efConstruction": 256, "metric_type": "COSINE"},
    )
    if _is_hybrid(desc):
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


def _assert_no_field_loss(source_desc: dict[str, Any], rebuilt_desc: dict[str, Any]) -> None:
    """Fail loudly if the rebuild would drop a declared field.

    Dynamic (undeclared) fields travel with the row payload, but a *declared*
    field this migration does not know about would be silently lost.
    """
    missing = set(_field_map(source_desc)) - set(_field_map(rebuilt_desc))
    if missing:
        raise RuntimeError(
            f"Rebuilt schema is missing declared field(s) {sorted(missing)} present in the source "
            "collection. This migration predates them — update it before running."
        )


# ---------------------------------------------------------------------------
# Row copy
# ---------------------------------------------------------------------------


def _copy_batch_size(dim: int) -> int:
    """Rows per page, kept under Milvus's result-size cap for wide vectors."""
    budget = 32 * 1024 * 1024
    per_row = dim * 4 + 6_144
    return max(1, min(MAX_COPY_BATCH, budget // per_row))


def _prepare_row(row: dict[str, Any]) -> dict[str, Any]:
    """Strip server-owned keys and normalise values for re-insertion."""
    out: dict[str, Any] = {}
    for key, value in row.items():
        if key in _UNSETTABLE_KEYS:
            continue
        out[key] = value.isoformat() if isinstance(value, datetime) else value
    return out


def _copy_rows(client: MilvusClient, source: str, target: str, dim: int) -> int:
    """Stream every row from ``source`` into ``target``. Returns rows inserted."""
    batch_size = _copy_batch_size(dim)
    client.load_collection(source)
    iterator = client.query_iterator(
        collection_name=source,
        filter="",
        batch_size=batch_size,
        output_fields=["*"],
    )
    copied = 0
    try:
        while True:
            batch = iterator.next()
            if not batch:
                break
            rows = [_prepare_row(r) for r in batch]
            client.insert(collection_name=target, data=rows)
            copied += len(rows)
            logger.info(f"  copied {copied} rows...")
    finally:
        iterator.close()
    client.flush(target)
    return copied


# ---------------------------------------------------------------------------
# Upgrade / downgrade
# ---------------------------------------------------------------------------


def _stamp_version(client: MilvusClient, collection_name: str, version: int) -> None:
    client.alter_collection_properties(
        collection_name=collection_name,
        properties={SCHEMA_VERSION_PROPERTY_KEY: str(version)},
    )
    logger.info(f"Stamped schema version {version} on '{collection_name}'.")


def upgrade(client: MilvusClient, collection_name: str, dry_run: bool = False) -> None:
    stored_version = _get_stored_version(client, collection_name)
    if stored_version >= TARGET_VERSION:
        logger.info(f"Collection is already at version {stored_version} — nothing to do.")
        return

    desc = client.describe_collection(collection_name)

    # Nothing to rebuild when there is no BM25 leg, or when the analyzer is
    # already correct — only the version stamp is missing.
    if not _is_hybrid(desc):
        logger.info("Collection has no `sparse` field (hybrid search disabled) — no BM25 analyzer to fix.")
        if not dry_run:
            _stamp_version(client, collection_name, TARGET_VERSION)
        return
    if _analyzer_has_lowercase(desc):
        logger.info("The `text` analyzer already applies the lowercase filter — no rebuild needed.")
        if not dry_run:
            _stamp_version(client, collection_name, TARGET_VERSION)
        return

    rebuild_name = f"{collection_name}{REBUILD_SUFFIX}"
    backup_name = f"{collection_name}{BACKUP_SUFFIX}"
    for name in (rebuild_name, backup_name):
        if client.has_collection(name):
            raise RuntimeError(
                f"Collection '{name}' already exists — a previous run of this migration was "
                "interrupted. Inspect it and drop it before retrying."
            )

    dim = _vector_dim(desc)
    source_count = _row_count(client, collection_name)
    logger.info(
        f"Rebuilding '{collection_name}' ({source_count} rows, dim={dim}) with the corrected analyzer.\n"
        f"  new collection : {rebuild_name}\n"
        f"  old kept as    : {backup_name}"
    )

    if dry_run:
        logger.info(f"[DRY-RUN] Would copy {source_count} rows and swap the collection names.")
        logger.info("[DRY-RUN] Dry-run complete. No changes were made.")
        return

    schema = _build_v2_schema(client, desc)
    index_params = _build_v2_index_params(client, desc)
    client.create_collection(
        collection_name=rebuild_name,
        schema=schema,
        consistency_level="Strong",
        index_params=index_params,
        enable_dynamic_field=True,
    )
    logger.info(f"Created '{rebuild_name}'.")

    try:
        _assert_no_field_loss(desc, client.describe_collection(rebuild_name))
        copied = _copy_rows(client, collection_name, rebuild_name, dim)
        target_count = _row_count(client, rebuild_name)
        logger.info(f"Copied {copied} rows; target collection reports {target_count} (source: {source_count}).")
        if target_count != source_count:
            raise RuntimeError(
                f"Row-count mismatch after copy: source={source_count}, target={target_count}. "
                f"The source collection is untouched; drop '{rebuild_name}' and retry."
            )
        _stamp_version(client, rebuild_name, TARGET_VERSION)
    except Exception:
        logger.error(
            f"Rebuild failed — '{collection_name}' is untouched. "
            f"Drop the partial collection '{rebuild_name}' before retrying."
        )
        raise

    client.rename_collection(collection_name, backup_name)
    client.rename_collection(rebuild_name, collection_name)
    client.load_collection(collection_name)
    logger.info(
        f"Migration complete. '{collection_name}' now uses the case-insensitive analyzer.\n"
        f"  The previous collection is kept as '{backup_name}' — drop it once validated:\n"
        f"    client.drop_collection('{backup_name}')"
    )


def downgrade(client: MilvusClient, collection_name: str, dry_run: bool = False) -> None:
    """Swap the v1 backup collection back into place.

    Rows indexed after the upgrade live only in the v2 collection, so this is a
    point-in-time rollback: the v2 collection is kept aside rather than dropped.
    """
    backup_name = f"{collection_name}{BACKUP_SUFFIX}"
    aside_name = f"{collection_name}{ROLLBACK_ASIDE_SUFFIX}"

    if not client.has_collection(backup_name):
        logger.error(
            f"No backup collection '{backup_name}' found — this rollback needs the collection "
            "the upgrade set aside. Without it the only way back is to re-create the collection "
            "with the old analyzer and re-index."
        )
        return
    if client.has_collection(aside_name):
        raise RuntimeError(f"Collection '{aside_name}' already exists — inspect and drop it before rolling back.")

    logger.info(
        f"{'[DRY-RUN] ' if dry_run else ''}Rolling back:\n"
        f"  '{collection_name}' → '{aside_name}' (v2, kept aside)\n"
        f"  '{backup_name}' → '{collection_name}' (v1, restored)"
    )
    if dry_run:
        logger.info("[DRY-RUN] Dry-run complete. No changes were made.")
        return

    client.rename_collection(collection_name, aside_name)
    client.rename_collection(backup_name, collection_name)
    _stamp_version(client, collection_name, TARGET_VERSION - 1)
    client.load_collection(collection_name)
    logger.warning(
        f"Rollback complete. Rows indexed after the upgrade remain only in '{aside_name}' and are NOT "
        "in the restored collection."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Milvus migration: case-insensitive BM25 analyzer (v1 → v2)")
    parser.add_argument("--dry-run", action="store_true", help="Inspect only, make no changes")
    parser.add_argument(
        "--downgrade",
        action="store_true",
        help="Swap the v1 backup collection back into place",
    )
    args = parser.parse_args()

    cfg = load_config()
    host = cfg.vectordb.host
    port = cfg.vectordb.port
    collection_name = cfg.vectordb.collection_name
    uri = f"http://{host}:{port}"

    logger.info(f"Connecting to Milvus at {uri}, collection='{collection_name}'")
    client = MilvusClient(uri=uri)

    if not client.has_collection(collection_name):
        logger.error(f"Collection '{collection_name}' does not exist. Aborting.")
        sys.exit(1)

    if args.downgrade:
        downgrade(client, collection_name, dry_run=args.dry_run)
    else:
        upgrade(client, collection_name, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
