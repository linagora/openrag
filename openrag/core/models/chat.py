"""What a chat request asks for, read once from its ``metadata``.

``ChatOptions.from_metadata`` is the only place that interprets the metadata
flags, so the services don't each re-read the dict with their own rule. Each
flag keeps the reading it has always had: the diagnostic and retrieval flags
count only a JSON ``true`` (``is True``), while ``websearch``,
``use_map_reduce`` and ``spoken_style_answer`` accept any truthy value.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RetrievalOverrides:
    """Per-request diagnostic retrieval settings (``metadata.retrieval_*``)."""

    top_k: Any = None
    similarity_threshold: float | None = None
    disable_reranker: bool = False
    disable_expansion: bool = False

    @classmethod
    def from_metadata(cls, metadata: Mapping[str, Any]) -> RetrievalOverrides:
        return cls(
            top_k=metadata.get("retrieval_top_k"),
            similarity_threshold=_threshold(metadata.get("retrieval_similarity_threshold")),
            disable_reranker=metadata.get("retrieval_disable_reranker") is True,
            disable_expansion=metadata.get("retrieval_disable_expansion") is True,
        )

    def as_options(self) -> dict[str, object]:
        """Only the settings the request changes, keyed as the retrieval service takes them."""
        options: dict[str, object] = {}
        if self.top_k is not None:
            options["top_k"] = self.top_k
        if self.similarity_threshold is not None:
            options["similarity_threshold"] = self.similarity_threshold
        if self.disable_reranker:
            options["disable_reranker"] = True
        if self.disable_expansion:
            options["disable_expansion"] = True
        return options


def _threshold(value: Any) -> float | None:
    """``retrieval_similarity_threshold`` as a number; ``None`` when it is absent or not one.

    The chat schema refuses a value that isn't a number with a 422 before it gets
    here. Text completions don't validate it, and don't use the retrieval
    overrides: such a value is ignored there, not a 500.
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _attachment_ids(metadata: Mapping[str, Any]) -> tuple[str, ...]:
    """file_ids from ``metadata.attachments = [{"id": ...}, ...]``; malformed entries dropped."""
    raw = metadata.get("attachments")
    if not isinstance(raw, list):
        return ()
    return tuple(a["id"] for a in raw if isinstance(a, dict) and isinstance(a.get("id"), str) and a["id"])


@dataclass(frozen=True)
class ChatOptions:
    """The ``metadata`` flags of a chat or text-completion request."""

    # Sources
    websearch: bool = False
    workspace: Any = None
    attachment_ids: tuple[str, ...] = ()
    #: The request sent ``attachments`` at all: ``extra.attachments`` then reports which were searched.
    attachments_sent: bool = False
    use_map_reduce: bool = False
    # Query
    require_retrieval: bool = False
    bypass_query_contextualization: bool = False
    retrieval: RetrievalOverrides = field(default_factory=RetrievalOverrides)
    # Answer
    spoken_style_answer: bool = False
    # Debug
    include_retrieval_trace: bool = False
    compare_original_query: bool = False
    include_all_retrieved_sources: bool = False

    @classmethod
    def from_metadata(cls, metadata: Mapping[str, Any] | None) -> ChatOptions:
        """Read *metadata*."""
        metadata = metadata or {}
        return cls(
            websearch=bool(metadata.get("websearch", False)),
            workspace=metadata.get("workspace"),
            attachment_ids=_attachment_ids(metadata),
            attachments_sent=bool(metadata.get("attachments")),
            use_map_reduce=bool(metadata.get("use_map_reduce", False)),
            require_retrieval=metadata.get("require_retrieval") is True,
            bypass_query_contextualization=metadata.get("bypass_query_contextualization") is True,
            retrieval=RetrievalOverrides.from_metadata(metadata),
            spoken_style_answer=bool(metadata.get("spoken_style_answer", False)),
            include_retrieval_trace=metadata.get("include_retrieval_trace") is True,
            compare_original_query=metadata.get("compare_original_query") is True,
            include_all_retrieved_sources=metadata.get("include_all_retrieved_sources") is True,
        )

    @property
    def query_mode(self) -> str:
        """Which query the retrieval trace reports: ``compare``, ``original`` or ``contextualized``."""
        if self.compare_original_query:
            return "compare"
        if self.bypass_query_contextualization:
            return "original"
        return "contextualized"


__all__ = [
    "ChatOptions",
    "RetrievalOverrides",
]
