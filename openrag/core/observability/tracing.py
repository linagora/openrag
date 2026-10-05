"""Request tracing, exported to Langfuse.

Each traced API request (a chat turn, a text completion, a search) is one
trace. Under its root, every retrieval step is an observation: query
contextualization, each sub-query's vector search with its scored
candidates, reranking, fusion, the context the LLM was given, and each LLM
or embedding call with its model and token usage.

Tracing is off unless ``LANGFUSE_PUBLIC_KEY`` and ``LANGFUSE_SECRET_KEY`` are
set. The Langfuse SDK reads them, together with ``LANGFUSE_BASE_URL``,
``LANGFUSE_SAMPLE_RATE``, ``LANGFUSE_TRACING_ENVIRONMENT`` and
``LANGFUSE_RELEASE``. When it is off, every helper here is a no-op that never
initialises the SDK.

Only API entry points open a trace (:func:`start_trace`). Every other helper
records only inside one, so the LLM client, the embedder and the searcher
record nothing when an indexing actor or a background task calls them.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from functools import lru_cache
from typing import Any

from opentelemetry import trace as otel_trace

#: Characters of chunk text kept in a candidate list: enough to recognise a
#: passage in the UI, without copying whole documents into every trace.
TEXT_PREVIEW_CHARS = 160


class _NoopObservation:
    """Stands in for an observation when nothing is being recorded."""

    id = None
    trace_id = None

    def update(self, **_: Any) -> _NoopObservation:
        return self

    def end(self, **_: Any) -> _NoopObservation:
        return self


NOOP = _NoopObservation()


@lru_cache(maxsize=1)
def _client() -> Any | None:
    """The Langfuse client, or ``None`` when tracing is not configured."""
    if os.environ.get("LANGFUSE_TRACING_ENABLED", "true").lower() == "false":
        return None
    if not (os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")):
        return None
    from langfuse import get_client

    return get_client()


def enabled() -> bool:
    """Whether traces are exported at all."""
    return _client() is not None


def recording() -> bool:
    """Whether the current request is being traced (and sampled)."""
    return otel_trace.get_current_span().is_recording()


def new_trace_id(seed: str | None = None) -> str | None:
    """A trace id to hand out before the trace starts (e.g. in a response header).

    With a ``seed`` (the request id) the id is deterministic, so a request id
    found in the logs leads to its trace.
    """
    client = _client()
    return client.create_trace_id(seed=seed or None) if client is not None else None


@contextmanager
def start_trace(
    name: str,
    *,
    input: Any = None,
    trace_id: str | None = None,
    user_id: str | None = None,
    tags: Sequence[str] = (),
    metadata: dict[str, str] | None = None,
    version: str | None = None,
) -> Iterator[Any]:
    """Open the root observation of a request's trace.

    ``metadata`` is propagated to every observation of the trace, so Langfuse
    restricts it to short strings (coerced, at most 200 characters each).
    """
    client = _client()
    if client is None:
        yield NOOP
        return
    from langfuse import propagate_attributes

    trace_context = {"trace_id": trace_id} if trace_id else None
    with client.start_as_current_observation(
        name=name, as_type="span", input=input, trace_context=trace_context
    ) as root:
        with propagate_attributes(
            trace_name=name,
            user_id=user_id,
            tags=list(tags) or None,
            metadata=metadata,
            version=version,
        ):
            yield root


@contextmanager
def observe(name: str, *, as_type: str = "span", **attributes: Any) -> Iterator[Any]:
    """An observation nested under the current one, active for the block.

    Outside a recorded trace it yields :data:`NOOP` and records nothing.
    ``attributes`` are those of ``start_as_current_observation`` (``input``,
    ``metadata``, ``model``, ``model_parameters``, ...).
    """
    if not recording():
        yield NOOP
        return
    with _client().start_as_current_observation(name=name, as_type=as_type, **attributes) as observation:
        yield observation


def start(name: str, *, as_type: str = "span", **attributes: Any) -> Any:
    """Like :func:`observe`, but not made current, for work spanning a generator.

    The caller must ``end()`` it. Nothing can nest under it through the
    context, which is what a streamed LLM call needs: its body yields to
    the consumer between chunks.
    """
    if not recording():
        return NOOP
    return _client().start_observation(name=name, as_type=as_type, **attributes)


def event(name: str, **attributes: Any) -> None:
    """A point-in-time observation, e.g. a fallback that was taken."""
    if recording():
        _client().create_event(name=name, **attributes)


def flush() -> None:
    client = _client()
    if client is not None:
        client.flush()


# ---------------------------------------------------------------------------
# Payload helpers: what a candidate list looks like in a trace
# ---------------------------------------------------------------------------


def describe_chunks(
    chunks: Sequence[Any],
    *,
    scores: Sequence[float | None] | None = None,
    with_text: bool = True,
) -> list[dict[str, Any]]:
    """One entry per chunk, in rank order: its identity, score and a text preview.

    ``scores`` gives a score per chunk (e.g. the vector-search score, which
    the domain ``Chunk`` does not carry). A ``ScoredChunk``'s rerank score is
    read from the chunk itself. Works with domain chunks and with LangChain
    documents built from them.
    """
    described = []
    for rank, chunk in enumerate(chunks, start=1):
        metadata = getattr(chunk, "metadata", None) or {}
        text = getattr(chunk, "text", None)
        if text is None:
            text = getattr(chunk, "page_content", "")
        entry: dict[str, Any] = {
            "rank": rank,
            "chunk_id": getattr(chunk, "id", None) or metadata.get("_id"),
            "file_id": getattr(chunk, "document_id", None) or metadata.get("file_id"),
            "partition": getattr(chunk, "partition", None) or metadata.get("partition"),
            "filename": metadata.get("filename"),
        }
        if scores is not None and rank <= len(scores) and scores[rank - 1] is not None:
            entry["score"] = scores[rank - 1]
        rerank_score = getattr(chunk, "rerank_score", None)
        if rerank_score is None:
            rerank_score = metadata.get("rerank_score")
        if rerank_score is not None:
            entry["rerank_score"] = rerank_score
        if with_text:
            entry["text"] = text[:TEXT_PREVIEW_CHARS]
        described.append(entry)
    return described


def fingerprint(config: dict[str, Any]) -> str:
    """A short, stable hash of a configuration, to tell runs apart at a glance."""
    canonical = json.dumps(config, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:12]


__all__ = [
    "NOOP",
    "TEXT_PREVIEW_CHARS",
    "describe_chunks",
    "enabled",
    "event",
    "fingerprint",
    "flush",
    "new_trace_id",
    "observe",
    "recording",
    "start",
    "start_trace",
]
