"""
Generic Milvus migration runner
================================
Discovers all migration scripts in this directory (files matching ``N.*.py``),
sorts them by their numeric prefix, and runs ``upgrade()`` / ``downgrade()``
in order based on the current schema version stored in the collection.

Usage (from repo root, inside the container):

    # Dry-run — inspect what would change, no writes:
    docker compose run --no-deps --rm --build --entrypoint "" openrag \\
        uv run python services/persistence/migrations/milvus/migrate.py --dry-run

    # Upgrade to latest:
    docker compose run --no-deps --rm --entrypoint "" openrag \\
        uv run python services/persistence/migrations/milvus/migrate.py

    # Upgrade to a specific version:
    docker compose run --no-deps --rm --entrypoint "" openrag \\
        uv run python services/persistence/migrations/milvus/migrate.py --target 2

    # Downgrade to version 0 (resets version property, drops indexes):
    docker compose run --no-deps --rm --entrypoint "" openrag \\
        uv run python services/persistence/migrations/milvus/migrate.py --downgrade --target 0

Convention — each migration module must expose:
    TARGET_VERSION: int          # the version this script brings the DB to
    upgrade(client, collection_name, dry_run=False)
    downgrade(client, collection_name, dry_run=False)
"""

import argparse
import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

from config import load_config
from core.utils.logging import get_logger
from pymilvus import MilvusClient
from services.storage.milvus_store import SCHEMA_VERSION_PROPERTY_KEY

logger = get_logger()

_MIGRATION_PATTERN = re.compile(r"^(\d+)\..+\.py$")
_MIGRATIONS_DIR = Path(__file__).parent


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _discover_migrations() -> list[tuple[int, Path]]:
    """Return (version, path) pairs sorted by version for all migration files."""
    found: list[tuple[int, Path]] = []
    for p in _MIGRATIONS_DIR.iterdir():
        if p.name == Path(__file__).name:
            continue  # skip this runner
        m = _MIGRATION_PATTERN.match(p.name)
        if m:
            found.append((int(m.group(1)), p))
    found.sort(key=lambda x: x[0])
    return found


def _load_module(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(path.stem, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load migration module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _validate_module(module: ModuleType, path: Path) -> None:
    for attr in ("TARGET_VERSION", "upgrade", "downgrade"):
        if not hasattr(module, attr):
            raise AttributeError(f"Migration '{path.name}' is missing required attribute '{attr}'")


# ---------------------------------------------------------------------------
# Version helpers
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


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_upgrade(
    client: MilvusClient,
    collection_name: str,
    migrations: list[tuple[int, Path]],
    target_version: int,
    dry_run: bool,
) -> None:
    current = _get_stored_version(client, collection_name)
    logger.info(f"Current schema version: {current}  →  target: {target_version}")

    pending = [(v, p) for v, p in migrations if current < v <= target_version]
    if not pending:
        logger.info("Collection is already up to date — nothing to do.")
        return

    for version, path in pending:
        logger.info(f"--- Applying migration {path.name} (v{version}) ---")
        module = _load_module(path)
        _validate_module(module, path)
        module.upgrade(client, collection_name, dry_run=dry_run)

    logger.info("All pending migrations applied.")


def run_downgrade(
    client: MilvusClient,
    collection_name: str,
    migrations: list[tuple[int, Path]],
    target_version: int,
    dry_run: bool,
) -> None:
    current = _get_stored_version(client, collection_name)
    logger.info(f"Current schema version: {current}  →  downgrade target: {target_version}")

    # Run in reverse: undo the highest version first
    pending = [(v, p) for v, p in migrations if target_version < v <= current]
    pending.sort(key=lambda x: x[0], reverse=True)

    if not pending:
        logger.info("Nothing to downgrade.")
        return

    for version, path in pending:
        logger.info(f"--- Reverting migration {path.name} (v{version}) ---")
        module = _load_module(path)
        _validate_module(module, path)
        module.downgrade(client, collection_name, dry_run=dry_run)

    logger.info("Downgrade complete.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    migrations = _discover_migrations()
    latest_version = migrations[-1][0] if migrations else 0

    parser = argparse.ArgumentParser(
        description="Generic Milvus migration runner — discovers and applies all pending migrations."
    )
    parser.add_argument("--dry-run", action="store_true", help="Inspect only, make no changes")
    parser.add_argument(
        "--downgrade",
        action="store_true",
        help="Run downgrade instead of upgrade",
    )
    parser.add_argument(
        "--target",
        type=int,
        default=None,
        help=f"Target schema version (default: {latest_version} for upgrade, 0 for downgrade)",
    )
    args = parser.parse_args()

    # Resolve default target
    if args.target is None:
        target_version = 0 if args.downgrade else latest_version
    else:
        target_version = args.target

    if not migrations:
        logger.warning("No migration files found in this directory.")
        sys.exit(0)

    logger.info(f"Discovered {len(migrations)} migration(s): {[p.name for _, p in migrations]}")

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
        run_downgrade(client, collection_name, migrations, target_version, dry_run=args.dry_run)
    else:
        run_upgrade(client, collection_name, migrations, target_version, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
