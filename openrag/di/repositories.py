"""Factory for the :class:`CatalogStore` adapter.

The container calls :func:`create_catalog_store` once at startup to build the
concrete :class:`~services.storage.postgres_store.PostgresStore`. Centralising
construction here keeps two pieces of knowledge out of the container itself:

* **Database-name fallback.** The legacy ``MilvusDB`` actor derives the
  Postgres database name from the Milvus collection name
  (``partitions_for_collection_<collection>``) at ``vectordb.py:238``. The
  factory keeps that contract so wiring code never has to mention the
  ``partitions_for_collection_`` prefix.
* **Migration trigger.** The factory always builds a store that will run
  Alembic at :meth:`PostgresStore.initialize` unless the caller opts out via
  ``run_migrations=False`` (useful in tests against a pre-migrated database).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from services.storage.postgres_store import PostgresStore

if TYPE_CHECKING:
    from core.config.root import Settings
    from core.ports.catalog_store import CatalogStore


def create_catalog_store(
    settings: Settings,
    *,
    run_migrations: bool | None = None,
) -> CatalogStore:
    """Build the relational catalog store from the root settings.

    When ``settings.rdb.database`` is unset the database name is derived from
    ``settings.vectordb.collection_name`` so the new adapter targets the same
    Postgres database the legacy actor has always used.
    """
    rdb = settings.rdb
    if rdb.database is None:
        rdb = rdb.model_copy(
            update={
                "database": f"partitions_for_collection_{settings.vectordb.collection_name}",
            },
        )
    if run_migrations is None:
        run_migrations = getattr(rdb, "run_migrations", True)
    return PostgresStore(rdb, run_migrations=run_migrations)


__all__ = ["create_catalog_store"]
