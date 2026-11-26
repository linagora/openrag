"""Tests for the Embedder port's defaults."""

import pytest


@pytest.mark.asyncio
async def test_embed_query_defaults_to_embed(mock_embedder):
    """An embedder without a query budget of its own embeds queries like any text."""
    vectors = await mock_embedder.embed_query(["q1", "q22"])

    assert vectors == await mock_embedder.embed(["q1", "q22"])
    assert mock_embedder.embed_calls == [["q1", "q22"], ["q1", "q22"]]
