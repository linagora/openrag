"""
Milvus migration: one dense field per embedder  (schema version 2 → 3)
======================================================================
Moves every chunk's vector out of the shared ``vector`` field into the field of
the embedder its partition uses (``vector_<embedder>``), then drops ``vector``.

One shared field put every embedder's output in one index, which is what made an
embedder swap silently corrupt retrieval (#762). From version 3 each embedder
owns a field and a search reads only its own.

In place, no rebuild. The steps, each verified against Milvus 3.0.1:

1. **Plan from the catalog.** Which field a partition's rows go to is recorded
   in Postgres, not Milvus: ``partitions.embedder`` names an endpoint, and that
   endpoint's ``model_endpoints.vector_field`` names the field. The SQL
   migration ``c4e8f2a6b913`` allocates those names; it runs when OpenRAG
   starts, or standalone with ``python -m services.persistence.migrations.run``,
   and must have run before this. A partition still on the ``default`` alias
   resolves to the current default embedder.
2. **Add each field** — nullable, with the same HNSW/COSINE recipe as ``vector``
   and its dimension, since the vectors are copied rather than re-embedded.
3. **Copy per partition** with a partial upsert of ``{_id, <field>}``. Chunk IDs,
   text, metadata and the BM25 ``sparse`` vector are untouched.
4. **Verify by scanning.** Milvus rejects ``IS NULL`` on vector fields, so each
   copied partition is read back and its non-null count compared.
5. **Drop ``vector``** and stamp version 3.

A re-run after a failure is safe: fields that exist are reused, and copying the
same vector again is a no-op. Only the final drop is irreversible, and it runs
after verification.

Rows whose partition is not in Postgres cannot be routed. They are unreachable
from OpenRAG already; they keep their text and metadata and lose the dense
vector, and the migration logs how many there are.

OpenRAG must be stopped: a row written while the copy runs could land in
``vector`` after its partition was copied. The migration re-counts the
collection before the drop and aborts if it moved.

Usage — prefer the generic runner (from repo root, inside the container). It
reads Postgres as well as Milvus, so both must be up (``docker compose up -d rdb
milvus``) even though ``--no-deps`` does not start them:
    docker compose run --no-deps --rm --entrypoint "" openrag \\
        uv run python services/persistence/migrations/milvus/migrate.py [--dry-run]
"""

import argparse
import asyncio
import json
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from core.config import load_config
from core.utils.logging import get_logger
from pymilvus import DataType, MilvusClient
from services.storage.milvus_store import SCHEMA_VERSION_PROPERTY_KEY

TARGET_VERSION = 3

LEGACY_FIELD = "vector"
DEFAULT_ALIAS = "default"

# Frozen snapshot of the dense index recipe — importing the live one would let a
# later tuning change rewrite what this migration builds.
DENSE_INDEX = {"index_type": "HNSW", "metric_type": "COSINE", "params": {"M": 128, "efConstruction": 256}}

#: Milvus caps a collection at ten vector fields, the sparse BM25 one included.
MAX_VECTOR_FIELDS = 10

_VECTOR_TYPES = frozenset(
    {
        DataType.BINARY_VECTOR,
        DataType.FLOAT_VECTOR,
        DataType.FLOAT16_VECTOR,
        DataType.BFLOAT16_VECTOR,
        DataType.SPARSE_FLOAT_VECTOR,
        DataType.INT8_VECTOR,
    }
)

#: How long to wait for index builds after the copy before carrying on.
INDEX_WAIT_SECONDS = 300.0

#: Upper bound on rows per copy page, further capped by the vector payload.
MAX_COPY_BATCH = 1_000
_COPY_PAGE_BUDGET_BYTES = 32 * 1024 * 1024

logger = get_logger()


# ---------------------------------------------------------------------------
# Catalog (Postgres)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Catalog:
    #: partition name → embedder reference as stored (a name, or the alias)
    partitions: dict[str, str]
    #: embedder name → allocated field, or None when the SQL migration has not run
    embedder_fields: dict[str, str | None]
    default_embedder: str | None


async def _fetch_catalog(collection_name: str) -> Catalog:
    import asyncpg

    rdb = load_config().rdb
    database = rdb.database or f"partitions_for_collection_{collection_name}"
    try:
        conn = await asyncpg.connect(
            host=rdb.host, port=rdb.port, user=rdb.user, password=rdb.password, database=database
        )
    except (OSError, asyncpg.PostgresError) as exc:
        # A bare socket.gaierror says nothing about why a *Milvus* migration
        # wants Postgres, and `run --no-deps` does not start it.
        raise RuntimeError(
            f"Cannot reach Postgres at {rdb.host}:{rdb.port} (database '{database}'): {exc}. This migration reads "
            "which embedder each partition uses from Postgres, so the database must be running — with Docker "
            "Compose: `docker compose up -d rdb`."
        ) from exc
    try:
        partitions = await conn.fetch("SELECT partition, embedder FROM partitions")
        embedders = await conn.fetch(
            "SELECT name, vector_field, is_default FROM model_endpoints WHERE model_type = 'embedder'"
        )
    except asyncpg.UndefinedColumnError as exc:
        raise RuntimeError(
            f"Postgres database '{database}' has no `model_endpoints.vector_field` yet: start the new OpenRAG "
            "version once so its SQL migrations run, stop it, then retry."
        ) from exc
    finally:
        await conn.close()
    defaults = [r["name"] for r in embedders if r["is_default"]]
    return Catalog(
        partitions={r["partition"]: r["embedder"] for r in partitions},
        embedder_fields={r["name"]: r["vector_field"] for r in embedders},
        default_embedder=defaults[0] if len(defaults) == 1 else None,
    )


def load_catalog(collection_name: str) -> Catalog:
    """Read the partition → embedder → field mapping. Replaced in tests."""
    return asyncio.run(_fetch_catalog(collection_name))


def _field_for_partition(catalog: Catalog, partition: str) -> tuple[str | None, str]:
    """The field a partition's rows belong in, or ``None`` and the reason."""
    reference = catalog.partitions[partition]
    name = catalog.default_embedder if reference == DEFAULT_ALIAS else reference
    if name is None:
        return None, f"partition '{partition}' uses the `default` alias but there is no single default embedder"
    if name not in catalog.embedder_fields:
        return None, f"partition '{partition}' uses embedder '{name}', which does not exist"
    target = catalog.embedder_fields[name]
    if target is None:
        return None, (
            f"embedder '{name}' has no vector field yet — start the new OpenRAG version once so its SQL "
            "migrations run, then retry"
        )
    return target, ""


# ---------------------------------------------------------------------------
# Introspection
# ---------------------------------------------------------------------------


def _get_stored_version(client: MilvusClient, collection_name: str) -> int:
    raw = client.describe_collection(collection_name).get("properties", {}).get(SCHEMA_VERSION_PROPERTY_KEY)
    try:
        return int(raw) if raw is not None else 0
    except ValueError:
        return 0


def _fields(desc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {f["name"]: f for f in desc.get("fields", [])}


def _dim(f: dict[str, Any]) -> int:
    return int(f.get("params", {}).get("dim", 0))


def _vector_field_count(desc: dict[str, Any]) -> int:
    return sum(1 for f in desc.get("fields", []) if f.get("type") in _VECTOR_TYPES)


def _dense_fields(desc: dict[str, Any]) -> dict[str, int]:
    """Per-embedder dense fields (everything dense but ``vector``) → dimension."""
    return {
        name: _dim(f)
        for name, f in _fields(desc).items()
        if f.get("type") == DataType.FLOAT_VECTOR and name != LEGACY_FIELD
    }


def _literal(value: str) -> str:
    """A Milvus string literal — JSON escaping is the escaping Milvus parses."""
    return json.dumps(value)


def _count(client: MilvusClient, collection_name: str, filter_expr: str = "") -> int:
    rows = client.query(collection_name=collection_name, filter=filter_expr, output_fields=["count(*)"])
    return int(rows[0]["count(*)"]) if rows else 0


def _batch_size(dim: int) -> int:
    return max(1, min(MAX_COPY_BATCH, _COPY_PAGE_BUDGET_BYTES // (dim * 4 + 64)))


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


@dataclass
class Plan:
    source: str
    dim: int
    total_rows: int
    #: target field → partitions (with at least one row) whose rows it receives
    moves: dict[str, list[str]] = field(default_factory=dict)
    row_counts: dict[str, int] = field(default_factory=dict)
    fields_to_add: list[str] = field(default_factory=list)

    @property
    def routed_rows(self) -> int:
        return sum(self.row_counts.values())


def _plan(
    client: MilvusClient,
    collection_name: str,
    catalog: Catalog,
    *,
    source: str,
    target_for: Any,
) -> Plan:
    """Work out every move before changing anything, and refuse what cannot be done.

    ``target_for(partition)`` returns ``(field, reason)`` — the upgrade routes
    by embedder, the downgrade routes everything to ``vector``.
    """
    desc = client.describe_collection(collection_name)
    fields = _fields(desc)
    client.load_collection(collection_name)
    total = _count(client, collection_name)

    problems: list[str] = []
    moves: dict[str, list[str]] = {}
    counts: dict[str, int] = {}
    for partition in sorted(catalog.partitions):
        rows = _count(client, collection_name, f"partition == {_literal(partition)}")
        if rows == 0:
            continue
        target, reason = target_for(partition)
        if target is None:
            problems.append(reason)
            continue
        moves.setdefault(target, []).append(partition)
        counts[partition] = rows

    # 0 when the source is absent — a downgrade re-creating `vector` — in which
    # case the caller settles the dimension.
    source_field = fields.get(source)
    dim = _dim(source_field) if source_field else 0
    fields_to_add = [target for target in moves if target not in fields]
    for target in moves:
        if target in fields and fields[target].get("type") != DataType.FLOAT_VECTOR:
            problems.append(f"field '{target}' exists but is not a FLOAT_VECTOR")
        elif target in fields and dim and _dim(fields[target]) != dim:
            problems.append(f"field '{target}' exists with dim {_dim(fields[target])}, but '{source}' has dim {dim}")
    if _vector_field_count(desc) + len(fields_to_add) > MAX_VECTOR_FIELDS:
        problems.append(
            f"adding {len(fields_to_add)} field(s) would exceed Milvus's limit of {MAX_VECTOR_FIELDS} vector "
            "fields per collection — delete unused embedders first"
        )
    if problems:
        raise RuntimeError("Cannot migrate:\n  - " + "\n  - ".join(problems))

    return Plan(source=source, dim=dim, total_rows=total, moves=moves, row_counts=counts, fields_to_add=fields_to_add)


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def _add_dense_field(client: MilvusClient, collection_name: str, name: str, dim: int) -> None:
    client.add_collection_field(
        collection_name=collection_name, field_name=name, data_type=DataType.FLOAT_VECTOR, dim=dim, nullable=True
    )
    logger.info(f"  added field '{name}' (dim={dim})")


def _ensure_dense_index(client: MilvusClient, collection_name: str, name: str) -> None:
    """Index ``name`` unless it already is — a re-run may find the field but not its index.

    Never waits for the build (``sync=False``). The rows already in the
    collection hold no value for a field added after them, and Milvus 3.0.1
    keeps a synchronous build of that field pending on them indefinitely —
    observed as ``indexed 0/3, pending 3`` for nine minutes. The copy rewrites
    those rows, and :func:`_wait_for_indexes` waits for the build afterwards.
    """
    if client.list_indexes(collection_name, field_name=name):
        return
    index_params = client.prepare_index_params()
    index_params.add_index(
        field_name=name,
        index_type=DENSE_INDEX["index_type"],
        metric_type=DENSE_INDEX["metric_type"],
        index_params={**DENSE_INDEX["params"], "metric_type": DENSE_INDEX["metric_type"]},
    )
    client.create_index(collection_name, index_params, sync=False)
    logger.info(f"  requested the index on '{name}'")


def _wait_for_indexes(client: MilvusClient, collection_name: str, names: Iterable[str], timeout: float) -> None:
    """Wait for the builds to catch up with the copied rows. Bounded, and never fatal.

    Searches work before a build finishes — Milvus scans what is not indexed
    yet — so an unfinished build costs latency, not correctness. It is logged
    rather than raised so a slow build cannot strand a verified copy.
    """
    deadline = time.monotonic() + timeout
    pending = set(names)
    while pending:
        for name in sorted(pending):
            (index_name,) = client.list_indexes(collection_name, field_name=name) or [name]
            info = client.describe_index(collection_name, index_name)
            if int(info.get("pending_index_rows", 0) or 0) == 0:
                logger.info(f"  index on '{name}': {info.get('indexed_rows')}/{info.get('total_rows')} rows")
                pending.discard(name)
        if pending and time.monotonic() >= deadline:
            logger.warning(
                f"  index build still running on {sorted(pending)} after {timeout:.0f}s. Searches work meanwhile; "
                "Milvus finishes it in the background."
            )
            return
        if pending:
            time.sleep(2)


def _iter_pages(client: MilvusClient, collection_name: str, filter_expr: str, output_field: str, dim: int):
    iterator = client.query_iterator(
        collection_name=collection_name,
        filter=filter_expr,
        batch_size=_batch_size(dim),
        output_fields=[output_field],
    )
    try:
        while True:
            page = iterator.next()
            if not page:
                return
            yield page
    finally:
        iterator.close()


def _copy_partition(
    client: MilvusClient, collection_name: str, partition: str, source: str, target: str, dim: int
) -> int:
    copied = 0
    for page in _iter_pages(client, collection_name, f"partition == {_literal(partition)}", source, dim):
        rows = [{"_id": row["_id"], target: row[source]} for row in page if row.get(source) is not None]
        if rows:
            client.upsert(collection_name=collection_name, data=rows, partial_update=True)
        copied += len(rows)
    return copied


def _non_null(client: MilvusClient, collection_name: str, partitions: Iterable[str], field_name: str, dim: int) -> int:
    names = ", ".join(_literal(p) for p in partitions)
    return sum(
        1
        for page in _iter_pages(client, collection_name, f"partition in [{names}]", field_name, dim)
        for row in page
        if row.get(field_name) is not None
    )


def _stamp(client: MilvusClient, collection_name: str, version: int) -> None:
    client.alter_collection_properties(
        collection_name=collection_name, properties={SCHEMA_VERSION_PROPERTY_KEY: str(version)}
    )
    logger.info(f"Stamped schema version {version} on '{collection_name}'.")


def _log_plan(plan: Plan, prefix: str) -> None:
    for target, partitions in plan.moves.items():
        rows = sum(plan.row_counts[p] for p in partitions)
        new = " (new field)" if target in plan.fields_to_add else ""
        logger.info(f"{prefix}  {plan.source} → {target}{new}: {rows} rows in {len(partitions)} partition(s)")
    unrouted = plan.total_rows - plan.routed_rows
    if unrouted:
        logger.warning(
            f"{prefix}  {unrouted} row(s) belong to partitions that do not exist in Postgres. They are already "
            f"unreachable from OpenRAG, and lose their `{plan.source}` value when that field is dropped."
        )


def upgrade(client: MilvusClient, collection_name: str, dry_run: bool = False) -> None:
    stored = _get_stored_version(client, collection_name)
    if stored >= TARGET_VERSION:
        logger.info(f"Collection is already at version {stored} — nothing to do.")
        return

    desc = client.describe_collection(collection_name)
    fields = _fields(desc)
    if LEGACY_FIELD not in fields:
        logger.info(f"'{collection_name}' has no `{LEGACY_FIELD}` field — only the version stamp is missing.")
        if not dry_run:
            _stamp(client, collection_name, TARGET_VERSION)
        return

    catalog = load_catalog(collection_name)
    plan = _plan(
        client,
        collection_name,
        catalog,
        source=LEGACY_FIELD,
        target_for=lambda partition: _field_for_partition(catalog, partition),
    )
    has_sparse = any(f.get("type") == DataType.SPARSE_FLOAT_VECTOR for f in fields.values())
    if not plan.moves and not has_sparse:
        raise RuntimeError(
            f"'{collection_name}' has no rows to route and no other vector field, and Milvus refuses a collection "
            f"without one, so `{LEGACY_FIELD}` cannot be dropped. It holds {plan.total_rows} row(s): if that is 0, "
            "drop the collection and let OpenRAG recreate it."
        )

    prefix = "[DRY-RUN] " if dry_run else ""
    logger.info(f"{prefix}Splitting `{LEGACY_FIELD}` of '{collection_name}' ({plan.total_rows} rows, dim={plan.dim}):")
    _log_plan(plan, prefix)
    if dry_run:
        logger.info("[DRY-RUN] Dry-run complete. No changes were made.")
        return

    for name in plan.fields_to_add:
        _add_dense_field(client, collection_name, name, plan.dim)
    for target in plan.moves:
        _ensure_dense_index(client, collection_name, target)
    client.refresh_load(collection_name)

    for target, partitions in plan.moves.items():
        for partition in partitions:
            copied = _copy_partition(client, collection_name, partition, LEGACY_FIELD, target, plan.dim)
            logger.info(f"  {partition}: copied {copied} row(s) into '{target}'")
    client.flush(collection_name)

    _verify(client, collection_name, plan)
    _wait_for_indexes(client, collection_name, plan.moves, INDEX_WAIT_SECONDS)

    client.drop_collection_field(collection_name, LEGACY_FIELD)
    logger.info(f"Dropped `{LEGACY_FIELD}`.")
    client.refresh_load(collection_name)
    _stamp(client, collection_name, TARGET_VERSION)
    logger.info("Migration complete.")


def _verify(client: MilvusClient, collection_name: str, plan: Plan) -> None:
    """Refuse the irreversible step unless every routed row has its vector."""
    final_total = _count(client, collection_name)
    if final_total != plan.total_rows:
        raise RuntimeError(
            f"The collection changed during the copy: {plan.total_rows} rows before, {final_total} after. "
            f"Something is still writing to '{collection_name}' — stop OpenRAG and re-run. Nothing has been "
            "dropped yet."
        )
    for target, partitions in plan.moves.items():
        expected = sum(plan.row_counts[p] for p in partitions)
        found = _non_null(client, collection_name, partitions, target, plan.dim)
        if found != expected:
            raise RuntimeError(
                f"'{target}' holds {found} vector(s) for its partitions, expected {expected}. "
                "Nothing has been dropped yet — re-run the migration."
            )
        logger.info(f"  verified '{target}': {found}/{expected}")


def downgrade(client: MilvusClient, collection_name: str, dry_run: bool = False) -> None:
    """Copy each partition's vectors back into a shared ``vector`` and drop the per-embedder fields.

    Only possible while every field in use has one dimension — they are going
    back into a single field. The per-embedder fields are dropped afterwards
    because the version-2 code reads rows with ``output_fields=["*"]`` and would
    hand them back as chunk metadata. ``vector`` comes back nullable; version 2
    always writes it, so that changes nothing for it.
    """
    desc = client.describe_collection(collection_name)
    fields = _fields(desc)
    dense = _dense_fields(desc)
    catalog = load_catalog(collection_name)

    routed = {}
    for partition in catalog.partitions:
        source, _ = _field_for_partition(catalog, partition)
        routed[partition] = source if source in dense else None
    plan = _plan(
        client,
        collection_name,
        catalog,
        source=LEGACY_FIELD,
        target_for=lambda partition: (LEGACY_FIELD, "")
        if routed[partition]
        else (None, f"partition '{partition}' has rows but no per-embedder field to copy back from"),
    )
    sources = {routed[p] for p in plan.row_counts}
    dims = {dense[s] for s in sources}
    if len(dims) > 1:
        raise RuntimeError(
            f"Cannot downgrade: partitions use fields of different dimensions ({sorted(dims)}), which cannot share "
            f"one `{LEGACY_FIELD}` field. Re-embed them with one model first."
        )
    dim = dims.pop() if dims else (next(iter(dense.values())) if dense else 0)
    if not dim:
        raise RuntimeError(f"Cannot downgrade: no dense field to take the `{LEGACY_FIELD}` dimension from.")
    plan.dim = dim

    prefix = "[DRY-RUN] " if dry_run else ""
    logger.info(f"{prefix}Merging per-embedder fields of '{collection_name}' back into `{LEGACY_FIELD}` (dim={dim}):")
    for partition in sorted(plan.row_counts):
        logger.info(f"{prefix}  {routed[partition]} → {LEGACY_FIELD}: {partition} ({plan.row_counts[partition]} rows)")
    logger.info(f"{prefix}  then drop: {sorted(dense)}")
    if dry_run:
        logger.info("[DRY-RUN] Dry-run complete. No changes were made.")
        return

    if LEGACY_FIELD not in fields:
        _add_dense_field(client, collection_name, LEGACY_FIELD, dim)
    _ensure_dense_index(client, collection_name, LEGACY_FIELD)
    client.refresh_load(collection_name)
    for partition in sorted(plan.row_counts):
        copied = _copy_partition(client, collection_name, partition, routed[partition], LEGACY_FIELD, dim)
        logger.info(f"  {partition}: copied {copied} row(s) into '{LEGACY_FIELD}'")
    client.flush(collection_name)

    _verify(client, collection_name, plan)
    _wait_for_indexes(client, collection_name, [LEGACY_FIELD], INDEX_WAIT_SECONDS)

    for name in sorted(dense):
        client.drop_collection_field(collection_name, name)
        logger.info(f"Dropped '{name}'.")
    client.refresh_load(collection_name)
    _stamp(client, collection_name, TARGET_VERSION - 1)
    logger.info("Downgrade complete.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Milvus migration: one dense field per embedder (v2 → v3)")
    parser.add_argument("--dry-run", action="store_true", help="Inspect only, make no changes")
    parser.add_argument("--downgrade", action="store_true", help="Merge the per-embedder fields back into `vector`")
    args = parser.parse_args()

    cfg = load_config()
    uri = f"http://{cfg.vectordb.host}:{cfg.vectordb.port}"
    collection_name = cfg.vectordb.collection_name
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
