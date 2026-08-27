"""Tests for the migration that enables STT model endpoints."""

from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def migration(monkeypatch):
    alembic_dir = (
        Path(__file__).resolve().parents[4] / "openrag" / "services" / "persistence" / "migrations" / "alembic"
    )
    monkeypatch.syspath_prepend(str(alembic_dir))
    return importlib.import_module(
        "services.persistence.migrations.alembic.versions.a8b9c0d1e2f3_add_stt_model_endpoints",
    )


class _FakeOp:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def get_bind(self):
        return object()

    def drop_constraint(self, *args, **kwargs) -> None:
        self.calls.append(("drop_constraint", args, kwargs))

    def create_check_constraint(self, *args, **kwargs) -> None:
        self.calls.append(("create_check_constraint", args, kwargs))

    def execute(self, statement) -> None:
        self.calls.append(("execute", (str(statement),), {}))


def _set_constraint(monkeypatch: pytest.MonkeyPatch, migration, sql: str | None) -> None:
    constraints = [] if sql is None else [{"name": "ck_model_endpoint_type", "sqltext": sql}]
    monkeypatch.setattr(
        migration,
        "inspect",
        lambda _bind: SimpleNamespace(get_check_constraints=lambda _table: constraints),
    )


def test_upgrade_adds_stt_to_the_model_endpoint_constraint(monkeypatch: pytest.MonkeyPatch, migration) -> None:
    fake_op = _FakeOp()
    monkeypatch.setattr(migration, "op", fake_op)
    monkeypatch.setattr(migration, "table_exists", lambda _table: True)
    _set_constraint(monkeypatch, migration, migration._PREVIOUS_MODEL_TYPE_IN)

    migration.upgrade()

    assert fake_op.calls == [
        ("drop_constraint", ("ck_model_endpoint_type", "model_endpoints"), {"type_": "check"}),
        ("create_check_constraint", ("ck_model_endpoint_type", "model_endpoints", migration._MODEL_TYPE_IN), {}),
    ]


def test_downgrade_restores_the_previous_constraint(monkeypatch: pytest.MonkeyPatch, migration) -> None:
    fake_op = _FakeOp()
    monkeypatch.setattr(migration, "op", fake_op)
    monkeypatch.setattr(migration, "table_exists", lambda _table: True)
    _set_constraint(monkeypatch, migration, migration._MODEL_TYPE_IN)

    migration.downgrade()

    assert "DELETE FROM model_endpoints WHERE model_type = 'stt'" in fake_op.calls[0][1][0]
    assert fake_op.calls[1:] == [
        ("drop_constraint", ("ck_model_endpoint_type", "model_endpoints"), {"type_": "check"}),
        (
            "create_check_constraint",
            ("ck_model_endpoint_type", "model_endpoints", migration._PREVIOUS_MODEL_TYPE_IN),
            {},
        ),
    ]
