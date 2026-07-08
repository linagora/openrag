"""Shared inspection helpers for idempotent Alembic migrations.

Needed because older deployments may already contain some objects when a
newer release applies migrations. Migration scripts should tolerate objects
already existing.
"""

from alembic import op
from sqlalchemy import inspect


def table_exists(table: str) -> bool:
    return table in inspect(op.get_bind()).get_table_names()


def column_exists(table: str, column: str) -> bool:
    if not table_exists(table):
        return False
    return any(c["name"] == column for c in inspect(op.get_bind()).get_columns(table))


def index_exists(table: str, index: str) -> bool:
    if not table_exists(table):
        return False
    return any(i["name"] == index for i in inspect(op.get_bind()).get_indexes(table))


def fk_exists(table: str, fk_name: str) -> bool:
    if not table_exists(table):
        return False
    return any(fk["name"] == fk_name for fk in inspect(op.get_bind()).get_foreign_keys(table))


def unique_constraint_exists(table: str, constraint_name: str) -> bool:
    if not table_exists(table):
        return False
    return any(uc["name"] == constraint_name for uc in inspect(op.get_bind()).get_unique_constraints(table))


def column_type_is(table: str, column: str, sa_type: type) -> bool:
    """Return True if `table.column` exists and its type is an instance of `sa_type`."""
    if not table_exists(table):
        return False
    for col in inspect(op.get_bind()).get_columns(table):
        if col["name"] == column:
            return isinstance(col["type"], sa_type)
    return False
