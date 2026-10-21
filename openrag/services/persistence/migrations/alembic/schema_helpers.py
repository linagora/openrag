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


def check_constraint_exists(table: str, constraint_name: str) -> bool:
    if not table_exists(table):
        return False
    return any(c["name"] == constraint_name for c in inspect(op.get_bind()).get_check_constraints(table))


def column_type_is(table: str, column: str, sa_type: type) -> bool:
    """Return True if `table.column` exists and its type is an instance of `sa_type`."""
    if not table_exists(table):
        return False
    for col in inspect(op.get_bind()).get_columns(table):
        if col["name"] == column:
            return isinstance(col["type"], sa_type)
    return False


def index_is_unique(table: str, index: str) -> bool:
    """Return True if `table.index` exists and was created as a unique index."""
    if not table_exists(table):
        return False
    return any(i["name"] == index and bool(i.get("unique")) for i in inspect(op.get_bind()).get_indexes(table))


def unique_constraints_on(table: str, columns: list[str]) -> list[str]:
    """Names of the unique constraints on `table` covering exactly `columns`."""
    if not table_exists(table):
        return []
    wanted = sorted(columns)
    return [
        uc["name"]
        for uc in inspect(op.get_bind()).get_unique_constraints(table)
        if uc["name"] and sorted(uc["column_names"]) == wanted
    ]


def foreign_keys_to(table: str, referred_table: str) -> list[str]:
    """Names of the foreign keys on `table` that reference `referred_table`."""
    if not table_exists(table):
        return []
    return [
        fk["name"]
        for fk in inspect(op.get_bind()).get_foreign_keys(table)
        if fk["name"] and fk["referred_table"] == referred_table
    ]
