from __future__ import annotations

import pytest


class _FakePool:
    def __init__(self):
        self.fetch_params: tuple | None = None

    async def fetch(self, query: str, *params):
        self.fetch_params = params
        return []


def test_hard_cap_lives_in_retriever_config():
    from config import load_config

    cap = load_config().retriever.max_ancestor_depth_cap
    assert isinstance(cap, int)
    assert cap > 0


@pytest.mark.asyncio
async def test_none_max_depth_is_clamped_to_hard_cap():
    from config import load_config
    from services.persistence.document_repo import PgDocumentRepository

    pool = _FakePool()
    repo = PgDocumentRepository(pool_getter=lambda: pool)

    assert await repo.get_file_ancestors(partition="p", file_id="missing", max_ancestor_depth=None) == []
    assert pool.fetch_params == ("missing", "p", int(load_config().retriever.max_ancestor_depth_cap))


@pytest.mark.asyncio
async def test_explicit_depth_above_cap_is_clamped():
    from config import load_config
    from services.persistence.document_repo import PgDocumentRepository

    cap = int(load_config().retriever.max_ancestor_depth_cap)
    pool = _FakePool()
    repo = PgDocumentRepository(pool_getter=lambda: pool)

    assert await repo.get_file_ancestors(partition="p", file_id="missing", max_ancestor_depth=cap * 10) == []
    assert pool.fetch_params == ("missing", "p", cap)
