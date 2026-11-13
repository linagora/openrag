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
from enum import StrEnum
from typing import Any


class RagMode(StrEnum):
    """How a partition-backed request finds its sources (``metadata.rag_mode``).

    ``classic`` runs the fixed pipeline: contextualize, retrieve, answer.
    ``agentic`` lets the model call search and read tools in a loop, then answer.
    """

    CLASSIC = "classic"
    AGENTIC = "agentic"


DEFAULT_RAG_MODE = RagMode.CLASSIC


def parse_rag_mode(value: object) -> RagMode:
    """``metadata.rag_mode`` as a ``RagMode``; ``None`` is the default.

    Raises ``ValueError`` for an unknown mode, never falling back silently to
    another mode.
    """
    if value is None:
        return DEFAULT_RAG_MODE
    try:
        return RagMode(value)
    except ValueError:
        choices = ", ".join(mode.value for mode in RagMode)
        raise ValueError(f"rag_mode must be one of: {choices}") from None


def check_agentic_request(metadata: Mapping[str, Any], *, client_tools: bool, structured_output: bool) -> None:
    """Refuse, with ``ValueError``, an ``agentic`` chat request asking for what the agent doesn't do.

    The agent runs its own tools, has no web search (its sources are the
    documents), summarizes with its own tool rather than ``use_map_reduce``,
    and answers in text. Each of these would otherwise be dropped without a
    word. A request in another mode is not checked.
    """
    if parse_rag_mode(metadata.get("rag_mode")) is not RagMode.AGENTIC:
        return
    profile = metadata.get("agent_profile")
    if profile is not None and not isinstance(profile, str):
        raise ValueError("agent_profile must be a string")
    if client_tools:
        raise ValueError("rag_mode 'agentic' runs its own tools: a request can't add tools to it.")
    if metadata.get("websearch"):
        raise ValueError("rag_mode 'agentic' doesn't search the web: drop websearch, or use rag_mode 'classic'.")
    if metadata.get("use_map_reduce"):
        raise ValueError(
            "rag_mode 'agentic' summarizes with its own tool: drop use_map_reduce, or use rag_mode 'classic'."
        )
    if structured_output:
        raise ValueError("rag_mode 'agentic' answers in text: drop response_format, or use rag_mode 'classic'.")


@dataclass(frozen=True)
class RetrievalOverrides:
    """Per-request diagnostic retrieval settings (``metadata.retrieval_*``)."""

    top_k: Any = None
    similarity_threshold: float | None = None
    disable_reranker: bool = False
    disable_expansion: bool = False

    @classmethod
    def from_metadata(cls, metadata: Mapping[str, Any]) -> RetrievalOverrides:
        threshold = metadata.get("retrieval_similarity_threshold")
        return cls(
            top_k=metadata.get("retrieval_top_k"),
            similarity_threshold=float(threshold) if threshold is not None else None,
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


def _attachment_ids(metadata: Mapping[str, Any]) -> tuple[str, ...]:
    """file_ids from ``metadata.attachments = [{"id": ...}, ...]``; malformed entries dropped."""
    raw = metadata.get("attachments")
    if not isinstance(raw, list):
        return ()
    return tuple(a["id"] for a in raw if isinstance(a, dict) and isinstance(a.get("id"), str) and a["id"])


@dataclass(frozen=True)
class ChatOptions:
    """The ``metadata`` flags of a chat or text-completion request."""

    rag_mode: RagMode = DEFAULT_RAG_MODE
    #: ``metadata.agent_profile``: the agent an ``agentic`` request runs; ``None`` for the configured default.
    agent_profile: str | None = None
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
        """Read *metadata*. ``rag_mode`` must already be valid: the request schema checks it."""
        metadata = metadata or {}
        return cls(
            rag_mode=parse_rag_mode(metadata.get("rag_mode")),
            agent_profile=metadata.get("agent_profile"),
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
    "DEFAULT_RAG_MODE",
    "ChatOptions",
    "RagMode",
    "RetrievalOverrides",
    "check_agentic_request",
    "parse_rag_mode",
]
