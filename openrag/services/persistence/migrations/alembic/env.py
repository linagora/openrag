import os
import sys
from logging.config import fileConfig

# Make modules alongside env.py (e.g. schema_helpers) importable from
# migration scripts regardless of the cwd alembic is invoked from.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from alembic import context
from core.config import load_config
from services.persistence.schema import metadata as target_metadata
from sqlalchemy import URL, engine_from_config, pool

rag_config = load_config()


# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# When the caller (typically ``ConnectionManager.run_migrations``) already
# wired a DSN into ``sqlalchemy.url``, defer to it. The default in
# ``alembic.ini`` is the placeholder ``driver://user:pass@localhost/dbname``
# from Alembic's template; we treat that and an empty value as "fall back to
# the OpenRAG config".
preset_url = config.get_main_option("sqlalchemy.url") or ""
if (not preset_url) or preset_url.startswith("driver://"):
    rdb_user = rag_config.rdb.user
    rdb_password = rag_config.rdb.password
    rdb_port = rag_config.rdb.port
    rdb_host = rag_config.rdb.host

    collection_name = rag_config.vectordb.collection_name

    database_url = URL.create(
        drivername="postgresql",
        username=rdb_user,
        password=rdb_password,
        host=rdb_host,
        port=rdb_port,
        database=f"partitions_for_collection_{collection_name}",
    )
    config.set_main_option(
        "sqlalchemy.url",
        database_url.render_as_string(hide_password=False),
    )

# Metadata target for autogenerate is imported from
# `services.persistence.schema` (metadata-only Table definitions).

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
