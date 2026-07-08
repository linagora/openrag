"""Phase 7F — cross-store full-cycle integration (Phase 7C handoff).

Phase 7B's :class:`MilvusVectorStore` is wired through DI now, but a real
cross-store cycle (``create partition → upsert pre-embedded chunks → search
→ delete``) still needs:

* a Milvus instance reachable from the test runner (Person B's
  ``test_milvus_store_integration.py`` already covers that piece in
  isolation), and
* a fixture that builds *both* stores against the same Milvus collection
  + Postgres database — the existing ``postgres_store`` fixture in
  ``conftest.py`` doesn't yet hand out a Milvus store.

The combined fixture lands as part of Phase 7C (shim) so the assertion
matches the legacy ``MilvusDB`` cross-store flow byte-for-byte. Until then
this test stays ``xfail(strict=True)`` so the day the fixture is added and
the body filled in, the unintended pass trips a clear failure.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


@pytest.mark.xfail(
    reason="cross-store fixture lands in Phase 7C — see REFACTORING_DECISION_LOG.md",
    strict=True,
)
async def test_cross_store_full_cycle():
    # The shape the eventual test will take:
    #   1. ``postgres_store.partition_repo`` creates a partition row.
    #   2. ``vector_store.upsert`` inserts pre-embedded chunks tagged with
    #      that partition.
    #   3. ``vector_store.search`` round-trips the embedding and returns the
    #      ids/text.
    #   4. ``postgres_store.partition_repo.delete_partition`` cascades the
    #      catalog rows; ``vector_store.delete`` clears the Milvus side.
    raise NotImplementedError("cross-store fixture lands in Phase 7C")
