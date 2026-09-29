"""
Milvus migration: one dense field per embedder  (schema version 2 → 3)
======================================================================
Moves every chunk's vector out of the shared ``vector`` field into the field of
the embedder its partition uses, then drops ``vector``. In place: chunk IDs,
text, metadata and the BM25 ``sparse`` vector are untouched.

1. **Plan.** Postgres records which embedder each partition uses
   (``partitions.embedder``) and which field that embedder owns
   (``model_endpoints.vector_field``, allocated by SQL revision
   ``c4e8f2a6b913``, which must have run first). Anything that cannot be
   routed aborts the migration before it changes anything.
2. **Add and index each field**, with ``vector``'s dimension.
3. **Copy** each partition's vectors with a partial upsert of ``{_id, <field>}``,
   plus the chunk's section IDs folded below 2**53 (see :data:`SECTION_ID_MASK`).
   A partial upsert keeps ``_id``, which a full-row upsert would reassign.
4. **Verify** by reading every copied partition back (Milvus rejects
   ``IS NULL`` on vector fields).
5. **Drop ``vector``** and stamp version 3.

Safe to re-run after a failure; only the final drop is irreversible. Rows whose
partition is not in Postgres are already unreachable and lose their vector.
OpenRAG must be stopped: the migration aborts if the row count moves.

The partial upsert rewrites every number in a chunk's dynamic field as a
float64, so any other integer above 2**53 in its metadata comes back rounded.
Postgres keeps the exact upload metadata of each file.

Usage — prefer the generic runner (from infra/compose, with DC and SVC set as in the upgrade guide:
``DC="docker compose"; SVC=openrag`` on a GPU host,
``DC="docker compose --profile cpu"; SVC=openrag-cpu`` on a CPU host; add ``-p <project>`` and
your ``-f`` overlays to DC if you start the stack with them). It
reads Postgres as well as Milvus, so both must be up (``$DC up -d rdb milvus``) even though
``--no-deps`` does not start them:
    $DC run --no-deps --rm --entrypoint "" "$SVC" \\
        uv run --no-dev python services/persistence/migrations/milvus/migrate.py [--dry-run]
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

#: Dynamic keys that link a chunk to its neighbours in the same file.
SECTION_ID_KEYS = ("section_id", "prev_section_id", "next_section_id")

#: A partial upsert rewrites every number in the dynamic field as a float64,
#: which rounds integers above 2**53. Section IDs used to be about 1.8e18, where
#: that rounding makes a few hundred neighbouring chunks share one ID, so the
#: copy keeps only their low 53 bits. Applied to all three keys, the links still
#: match, and a file's consecutive IDs stay distinct.
SECTION_ID_MASK = 2**53 - 1

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
    """The collection's schema version: 0 when it was never stamped.

    Raises ValueError when ``int()`` rejects the stamp or it is negative, rather
    than reading it as 0 and migrating a collection whose version is unknown.
    """
    raw = client.describe_collection(collection_name).get("properties", {}).get(SCHEMA_VERSION_PROPERTY_KEY)
    if raw is None:
        return 0
    unknown = (
        f"'{collection_name}' has schema version {raw!r}, which is not a version. Set the collection's real "
        f"version first: MilvusClient(uri).alter_collection_properties('{collection_name}', "
        f"properties={{'{SCHEMA_VERSION_PROPERTY_KEY}': '<version>'}}). Nothing was changed."
    )
    try:
        version = int(raw)
    except ValueError:
        raise ValueError(unknown) from None
    if version < 0:
        raise ValueError(unknown)
    return version


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

    def rows_into(self, target: str) -> int:
        return sum(self.row_counts[p] for p in self.moves[target])


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

    ``sync=False``: a synchronous build on a field added after the rows stays
    pending until the copy fills it. :func:`_wait_for_indexes` waits afterwards.
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


def _wait_for_indexes(client: MilvusClient, collection_name: str, plan: Plan, timeout: float) -> None:
    """Wait until each target's index covers the rows copied into it.

    Ready means ``indexed_rows`` has reached the copied count, not
    ``pending_index_rows`` reaching 0: the partial upsert leaves the superseded
    rows in their old segments, which Milvus neither indexes nor compacts away,
    so they stay pending indefinitely. Bounded, and fatal only when a build
    fails — Milvus scans what is not indexed yet, so a slow build costs latency.
    """
    deadline = time.monotonic() + timeout
    pending = {target: plan.rows_into(target) for target in plan.moves}
    while pending:
        for name, expected in sorted(pending.items()):
            (index_name,) = client.list_indexes(collection_name, field_name=name) or [name]
            info = client.describe_index(collection_name, index_name)
            if info.get("state") == "Failed":
                raise RuntimeError(
                    f"The index build on '{name}' failed — the Milvus log gives the `failReason`. Nothing has been "
                    "dropped yet: fix the cause, drop that index and re-run."
                )
            indexed = int(info.get("indexed_rows", 0) or 0)
            if indexed >= expected:
                logger.info(f"  index on '{name}' built: {indexed} row(s) indexed, {expected} copied")
                del pending[name]
        if pending and time.monotonic() >= deadline:
            logger.warning(
                f"  index build still running on {sorted(pending)} after {timeout:.0f}s. Searches work meanwhile; "
                "Milvus finishes it in the background."
            )
            return
        if pending:
            time.sleep(2)


def _iter_pages(client: MilvusClient, collection_name: str, filter_expr: str, output_fields: list[str], dim: int):
    iterator = client.query_iterator(
        collection_name=collection_name,
        filter=filter_expr,
        batch_size=_batch_size(dim),
        output_fields=output_fields,
    )
    try:
        while True:
            page = iterator.next()
            if not page:
                return
            yield page
    finally:
        iterator.close()


def _folded_section_ids(row: dict[str, Any]) -> dict[str, int]:
    """The row's section IDs as the copy writes them back — see :data:`SECTION_ID_MASK`."""
    return {key: row[key] & SECTION_ID_MASK for key in SECTION_ID_KEYS if isinstance(row.get(key), int)}


def _copy_partition(
    client: MilvusClient, collection_name: str, partition: str, source: str, target: str, dim: int
) -> int:
    copied = 0
    filter_expr = f"partition == {_literal(partition)}"
    for page in _iter_pages(client, collection_name, filter_expr, [source, *SECTION_ID_KEYS], dim):
        rows = [
            {"_id": row["_id"], target: row[source], **_folded_section_ids(row)}
            for row in page
            if row.get(source) is not None
        ]
        if rows:
            client.upsert(collection_name=collection_name, data=rows, partial_update=True)
        copied += len(rows)
    return copied


def _non_null(client: MilvusClient, collection_name: str, partitions: Iterable[str], field_name: str, dim: int) -> int:
    names = ", ".join(_literal(p) for p in partitions)
    return sum(
        1
        for page in _iter_pages(client, collection_name, f"partition in [{names}]", [field_name], dim)
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
        new = " (new field)" if target in plan.fields_to_add else ""
        logger.info(
            f"{prefix}  {plan.source} → {target}{new}: {plan.rows_into(target)} rows in {len(partitions)} partition(s)"
        )
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
    _wait_for_indexes(client, collection_name, plan, INDEX_WAIT_SECONDS)

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
        expected = plan.rows_into(target)
        found = _non_null(client, collection_name, partitions, target, plan.dim)
        if found != expected:
            raise RuntimeError(
                f"'{target}' holds {found} vector(s) for its partitions, expected {expected}. "
                "Nothing has been dropped yet — re-run the migration."
            )
        logger.info(f"  verified '{target}': {found}/{expected}")


def downgrade(client: MilvusClient, collection_name: str, dry_run: bool = False) -> None:
    """Copy each partition's vectors back into a shared ``vector`` and drop the per-embedder fields.

    Only possible while every field in use has one dimension. The per-embedder
    fields are dropped because version 2 would return them as chunk metadata.
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
    _wait_for_indexes(client, collection_name, plan, INDEX_WAIT_SECONDS)

    for name in sorted(dense):
        client.drop_collection_field(collection_name, name)
        logger.info(f"Dropped '{name}'.")
    client.refresh_load(collection_name)
    _stamp(client, collection_name, TARGET_VERSION - 1)
    logger.info("Downgrade complete.")


def _refuse_out_of_order(client: MilvusClient, collection_name: str, downgrade: bool) -> None:
    """Refuse a standalone run that would apply or revert this step out of order.

    Run on its own, this script applies or reverts only its own step, whatever
    version the collection is at: out of order, that skips the steps before it or
    undoes an older step under a newer one. ``migrate.py`` walks the steps in turn.
    """
    try:
        stored = _get_stored_version(client, collection_name)
    except ValueError as exc:
        logger.error(str(exc))
        sys.exit(2)
    if downgrade and stored < TARGET_VERSION:
        logger.error(
            f"'{collection_name}' is at schema version {stored}, below this script's version {TARGET_VERSION}: "
            "there is nothing for it to revert."
        )
        sys.exit(2)
    if downgrade and stored > TARGET_VERSION:
        logger.error(
            f"'{collection_name}' is at schema version {stored}; this script's --downgrade only reverts a collection "
            f"at version {TARGET_VERSION}. Use migrate.py --downgrade --target N, from the release that migrated it."
        )
        sys.exit(2)
    if not downgrade and stored < TARGET_VERSION - 1:
        logger.error(
            f"'{collection_name}' is at schema version {stored}; this script only upgrades a collection at version "
            f"{TARGET_VERSION - 1} or later. Use migrate.py, which applies the steps in turn."
        )
        sys.exit(2)


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
    _refuse_out_of_order(client, collection_name, args.downgrade)
    if args.downgrade:
        downgrade(client, collection_name, dry_run=args.dry_run)
    else:
        upgrade(client, collection_name, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
