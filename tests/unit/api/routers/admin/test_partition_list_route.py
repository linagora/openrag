"""Authorization + shaping tests for ``GET /partition/`` (list_existant_partitions).

The list route was reworked to return per-partition stored config +
``document_count`` (via ``PartitionService.list_partition_summaries``). These
tests pin the security-relevant branch: a regular user must see summaries for
*only* their own partitions, while the super-admin ``"all"`` sentinel lists
every partition.
"""

from __future__ import annotations

import pytest
from api.dependencies.auth import partitions_with_details
from api.routers.admin import partitions
from di.providers import get_partition_service
from fastapi import FastAPI


def _summary(name: str, **overrides) -> dict:
    base = {
        "partition": name,
        "description": "",
        "embedder": "default",
        "indexation_preset": "default",
        "retrieval_preset": "default",
        "dimension": 1024,
        "chat_history_depth": 0,
        "chat_llm": None,
        "created_at": "2026-01-01T00:00:00+00:00",
        "document_count": 0,
    }
    base.update(overrides)
    return base


class _FakeService:
    """Minimal partition service exposing only what the list route calls."""

    def __init__(self, summaries: dict[str, dict]) -> None:
        self._summaries = summaries

    async def list_partition_summaries(self) -> dict[str, dict]:
        return self._summaries


def _build_app(service: _FakeService, principal_partitions: list[dict]) -> FastAPI:
    app = FastAPI()
    app.include_router(partitions.router, prefix="/partition")
    app.dependency_overrides[partitions_with_details] = lambda: principal_partitions
    app.dependency_overrides[get_partition_service] = lambda: service
    return app


@pytest.mark.asyncio
async def test_list_returns_only_the_callers_partitions(async_client_factory):
    """A regular user gets summaries for only their partitions — never others."""
    summaries = {
        "legal": _summary("legal", document_count=3),
        "hr": _summary("hr", document_count=1),
        "secret": _summary("secret", document_count=9),  # NOT the caller's
    }
    principal = [
        {"partition": "legal", "role": "owner"},
        {"partition": "hr", "role": "viewer"},
    ]
    app = _build_app(_FakeService(summaries), principal)
    async with async_client_factory(app) as client:
        resp = await client.get("/partition/")

    assert resp.status_code == 200
    by_name = {r["partition"]: r for r in resp.json()["partitions"]}
    assert set(by_name) == {"legal", "hr"}  # no cross-partition leak
    assert "secret" not in by_name
    assert by_name["legal"]["role"] == "owner"  # caller's role surfaced
    assert by_name["hr"]["role"] == "viewer"
    assert by_name["legal"]["document_count"] == 3  # summary carried through


@pytest.mark.asyncio
async def test_super_admin_all_sentinel_lists_every_partition(async_client_factory):
    """The ``"all"`` sentinel (super-admin) returns every partition's summary."""
    summaries = {n: _summary(n) for n in ("legal", "hr", "secret")}
    principal = [{"partition": "all", "role": "owner"}]
    app = _build_app(_FakeService(summaries), principal)
    async with async_client_factory(app) as client:
        resp = await client.get("/partition/")

    assert resp.status_code == 200
    assert {r["partition"] for r in resp.json()["partitions"]} == {"legal", "hr", "secret"}


@pytest.mark.asyncio
async def test_list_falls_back_when_summary_missing(async_client_factory):
    """A partition the caller holds but absent from summaries still appears with a
    zero ``document_count`` (the ``summaries.get(name) or {...}`` guard — no KeyError)."""
    app = _build_app(_FakeService({}), [{"partition": "ghost", "role": "editor"}])
    async with async_client_factory(app) as client:
        resp = await client.get("/partition/")

    assert resp.status_code == 200
    assert resp.json()["partitions"] == [
        {"partition": "ghost", "document_count": 0, "role": "editor"},
    ]
