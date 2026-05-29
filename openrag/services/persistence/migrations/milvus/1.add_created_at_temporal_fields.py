"""
Milvus migration: add temporal fields  (schema version 0 → 1)
==============================================================
Adds the following TIMESTAMPTZ field (nullable) to the existing collection:
    - created_at

The field also gets an STL_SORT index.  After a successful upgrade the
collection's ``openrag.schema_version`` property is set to 1 so the application
no longer raises VDBSchemaMigrationRequiredError on startup.

Existing documents will retain null for these fields; new documents will have
them populated at index time by the application code.

Usage — prefer the generic runner (from repo root, inside the container):
    docker compose run --no-deps --rm --entrypoint "" openrag \\
        uv run python services/persistence/migrations/milvus/migrate.py [--dry-run] [--downgrade] [--target N]

Or run this script directly:
    # Dry-run first (inspect only, no changes):
    docker compose run --no-deps --rm --build --entrypoint "" openrag \\
        uv run python services/persistence/migrations/milvus/1.add_created_at_temporal_fields.py --dry-run

    # Apply:
    docker compose run --no-deps --rm --build --entrypoint "" openrag \\
        uv run python services/persistence/migrations/milvus/1.add_created_at_temporal_fields.py

    # Roll back indexes and reset version (fields cannot be dropped in Milvus):
    docker compose run --no-deps --rm --entrypoint "" openrag \\
        uv run python services/persistence/migrations/milvus/1.add_created_at_temporal_fields.py --downgrade
"""

import argparse
import sys

from core.config import load_config
from core.utils.logging import get_logger
from pymilvus import DataType, MilvusClient
from services.storage.milvus_store import SCHEMA_VERSION_PROPERTY_KEY

TARGET_VERSION = 1  # The schema version this migration brings the collection to.

# ---------------------------------------------------------------------------
# Declarative migration spec — edit here to change what gets added/removed.
# ---------------------------------------------------------------------------

FIELDS_2_ADD = [
    {"field_name": "created_at", "data_type": DataType.TIMESTAMPTZ, "nullable": True},
]

INDEXES_2_ADD = [
    {"field_name": "created_at", "index_type": "STL_SORT", "index_name": "created_at_idx"},
]

# ---------------------------------------------------------------------------

logger = get_logger()


def _get_existing_field_names(client: MilvusClient, collection_name: str) -> set[str]:
    desc = client.describe_collection(collection_name)
    return {f["name"] for f in desc["fields"]}


def _get_existing_index_names(client: MilvusClient, collection_name: str) -> set[str]:
    return set(client.list_indexes(collection_name))


def _get_stored_version(client: MilvusClient, collection_name: str) -> int:
    desc = client.describe_collection(collection_name)
    raw = desc.get("properties", {}).get(SCHEMA_VERSION_PROPERTY_KEY)
    if raw is None:
        return 0
    try:
        return int(raw)
    except ValueError:
        return 0


def _print_state(client: MilvusClient, collection_name: str, required_version: int) -> None:
    """Query and display the current state of fields, indexes and schema version."""
    desc = client.describe_collection(collection_name)
    field_map = {f["name"]: f for f in desc["fields"]}
    existing_indexes = _get_existing_index_names(client, collection_name)
    stored_version = _get_stored_version(client, collection_name)

    field_to_index = {idx["field_name"]: idx["index_name"] for idx in INDEXES_2_ADD}

    logger.info(f"--- Collection '{collection_name}' state ---")
    logger.info(f"  Schema version : stored={stored_version}  required={required_version}")
    logger.info("  Fields:")
    for field_spec in FIELDS_2_ADD:
        field_name = field_spec["field_name"]
        index_name = field_to_index.get(field_name)
        field_present = field_name in field_map
        index_present = index_name is not None and index_name in existing_indexes

        index_detail = ""
        if index_present:
            info = client.describe_index(collection_name=collection_name, index_name=index_name)
            index_detail = f" (index_type={info.get('index_type')})"

        index_status = f"OK{index_detail}" if index_present else ("N/A" if not index_name else "MISSING")
        logger.info(f"    {field_name}: field={'OK' if field_present else 'MISSING'} | index={index_status}")


def upgrade(client: MilvusClient, collection_name: str, dry_run: bool = False) -> None:
    stored_version = _get_stored_version(client, collection_name)
    if stored_version >= TARGET_VERSION:
        logger.info(f"Collection is already at version {stored_version} — nothing to do.")
        _print_state(client, collection_name, TARGET_VERSION)
        return

    existing_fields = _get_existing_field_names(client, collection_name)
    existing_indexes = _get_existing_index_names(client, collection_name)
    fields_added: list[str] = []

    for field_spec in FIELDS_2_ADD:
        field_name = field_spec["field_name"]
        if field_name in existing_fields:
            logger.info(f"Field '{field_name}' already exists — skipping.")
            continue

        logger.info(f"{'[DRY-RUN] ' if dry_run else ''}Adding field '{field_name}'.")
        if not dry_run:
            client.add_collection_field(collection_name=collection_name, **field_spec)
        fields_added.append(field_name)

    index_params = client.prepare_index_params()
    needs_index = False

    for idx_spec in INDEXES_2_ADD:
        field_name = idx_spec["field_name"]
        index_name = idx_spec["index_name"]

        if index_name in existing_indexes:
            logger.info(f"Index '{index_name}' already exists — skipping.")
            continue

        if field_name not in existing_fields and field_name not in fields_added:
            logger.warning(f"Field '{field_name}' could not be added — skipping index.")
            continue

        logger.info(f"{'[DRY-RUN] ' if dry_run else ''}Scheduling index '{index_name}' for '{field_name}'.")
        index_params.add_index(**idx_spec)
        needs_index = True

    if not dry_run:
        if needs_index:
            logger.info("Creating indexes...")
            client.create_index(collection_name=collection_name, index_params=index_params)
            logger.info("Indexes created.")

        # Bump stored version so the application stops raising VDBSchemaMigrationRequiredError.
        logger.info(f"Storing schema version {TARGET_VERSION} on collection '{collection_name}'.")
        client.alter_collection_properties(
            collection_name=collection_name,
            properties={SCHEMA_VERSION_PROPERTY_KEY: str(TARGET_VERSION)},
        )
        logger.info("Migration complete.")
    else:
        logger.info("Dry-run complete. No changes were made.")

    _print_state(client, collection_name, TARGET_VERSION)


def downgrade(client: MilvusClient, collection_name: str, dry_run: bool = False) -> None:
    """
    Milvus does NOT support dropping fields from an existing collection.
    The only way to fully roll back is to drop and recreate the collection,
    which would lose all data.

    This function only removes the indexes and resets the version property.
    """
    existing_indexes = _get_existing_index_names(client, collection_name)

    for idx_spec in INDEXES_2_ADD:
        index_name = idx_spec["index_name"]
        if index_name not in existing_indexes:
            logger.info(f"Index '{index_name}' does not exist — skipping.")
            continue

        logger.info(f"{'[DRY-RUN] ' if dry_run else ''}Dropping index '{index_name}'.")
        if not dry_run:
            client.drop_index(collection_name=collection_name, index_name=index_name)

    if not dry_run:
        client.alter_collection_properties(
            collection_name=collection_name,
            properties={SCHEMA_VERSION_PROPERTY_KEY: "0"},
        )
        logger.info("Schema version reset to 0.")

    logger.warning(
        f"Fields {[f['field_name'] for f in FIELDS_2_ADD]} cannot be dropped from Milvus. "
        "To fully remove them you would need to recreate the collection."
    )

    _print_state(client, collection_name, required_version=0)


def main() -> None:
    parser = argparse.ArgumentParser(description="Milvus migration: add temporal fields (v0 → v1)")
    parser.add_argument("--dry-run", action="store_true", help="Inspect only, make no changes")
    parser.add_argument(
        "--downgrade",
        action="store_true",
        help="Drop indexes and reset version (fields cannot be dropped in Milvus)",
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
