"""One embedding per query text and vector space for the length of a retrieval.

A chat retrieval runs one leg per partition group and per sub-query, and a leg
can search twice (the filterless fallback). Legs on the same vector space embed
the same text, so inside a method decorated with
:func:`sharing_query_embeddings` the first leg to ask for a text starts the
embed and the others wait on it: a slow or dead embedder costs the request one
query budget, not one per leg.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Awaitable, Callable, Hashable
from contextvars import ContextVar

from .embedder import Embedder

# (vector space, text) -> the embed call that covers the text, and the text's index in it.
_SharedCalls = dict[tuple[Hashable, str], tuple["asyncio.Future[list[list[float]]]", int]]

_shared_calls: ContextVar[_SharedCalls | None] = ContextVar("shared_query_embeddings", default=None)


def sharing_query_embeddings[**P, R](method: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """Share query embeddings between every :func:`embed_queries` call *method* makes.

    A call nested in another decorated method shares the outer one's
    embeddings. When the outermost call returns, an embed no leg is still
    waiting on (its legs failed or were cancelled) is cancelled.
    """

    @functools.wraps(method)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        if _shared_calls.get() is not None:
            return await method(*args, **kwargs)
        calls: _SharedCalls = {}
        token = _shared_calls.set(calls)
        try:
            return await method(*args, **kwargs)
        finally:
            _shared_calls.reset(token)
            for call in {call for call, _index in calls.values()}:
                if not call.done():
                    call.cancel()
                elif not call.cancelled():
                    # Every leg waiting on a failed embed has raised its error;
                    # retrieving it here keeps asyncio from logging it again.
                    call.exception()

    return wrapper


async def embed_queries(embedder: Embedder, space: Hashable, texts: list[str]) -> list[list[float]]:
    """``embedder.embed_query(texts)``, sharing each text's vector with the rest of the retrieval.

    *space* names the vectors the embedding is compared with: two embedders
    whose vectors share a space are the same model, so either one's embedding
    of a text serves both. Outside a shared retrieval this is a plain
    ``embed_query``.
    """
    calls = _shared_calls.get()
    if calls is None:
        return await embedder.embed_query(texts)
    missing = [text for text in dict.fromkeys(texts) if (space, text) not in calls]
    if missing:
        call = asyncio.ensure_future(embedder.embed_query(missing))
        for index, text in enumerate(missing):
            calls[(space, text)] = (call, index)
    vectors = []
    for text in texts:
        call, index = calls[(space, text)]
        # Shielded: a leg cancelled while it waits must not cancel the embed
        # the other legs wait on.
        vectors.append((await asyncio.shield(call))[index])
    return vectors
