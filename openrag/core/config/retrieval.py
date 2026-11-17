"""Retrieval, reranker, RAG (default mode, query rewriting), map-reduce, and web search configuration."""

from __future__ import annotations

import functools
import logging
from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from .base import ConfigMixin

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Reranker
# ---------------------------------------------------------------------------


class _BaseRerankerConfig(ConfigMixin):
    model_name: str = "Alibaba-NLP/gte-multilingual-reranker-base"
    top_k: int = Field(default=10, gt=0)  # RERANKER_TOP_K: top_n of presets that leave it unset
    api_key: str = Field(default="EMPTY", repr=False)
    timeout: float = 60.0
    semaphore: int = 5
    enabled: bool = True


class InfinityRerankerConfig(_BaseRerankerConfig):
    provider: Literal["infinity"] = "infinity"
    base_url: str = "http://reranker:7997"


class OpenAIRerankerConfig(_BaseRerankerConfig):
    provider: Literal["openai"] = "openai"
    base_url: str = "http://reranker:8000/v1"


class TEIRerankerConfig(_BaseRerankerConfig):
    provider: Literal["tei"] = "tei"
    base_url: str = "http://reranker:8080"


RerankerConfig = Annotated[
    InfinityRerankerConfig | OpenAIRerankerConfig | TEIRerankerConfig,
    Field(discriminator="provider"),
]


def _default_reranker_config() -> InfinityRerankerConfig:
    return InfinityRerankerConfig()


# ---------------------------------------------------------------------------
# Retriever
# ---------------------------------------------------------------------------


class RetrieverConfig(ConfigMixin):
    """Static retriever settings that no retrieval preset carries.

    ``type``, ``k_queries`` and ``combine`` (the removed ``multiQuery`` and
    ``hyde`` strategies) are ignored when a config file still sets them.
    """

    top_k: int = 50
    similarity_threshold: float = 0.6
    with_surrounding_chunks: bool = False
    include_related: bool = True
    include_ancestors: bool = True
    related_limit: int = 10
    max_ancestor_depth: int = 10
    # Absolute upper bound on ancestor-CTE recursion regardless of caller-
    # supplied depth — defends against cyclic parent_id chains that would
    # otherwise loop until the DB aborts the query. Operators can tune via
    # the retriever config; the default is well above realistic deep
    # document hierarchies.
    max_ancestor_depth_cap: int = 1000
    allow_filterless_fallback: bool = True
    # Upper bound on how many partition-group retrievals run concurrently within
    # a single request. A multi-partition search (notably a SUPER_ADMIN_MODE
    # ``openrag-all`` that expands to every partition) fans out one embed+search
    # per group of partitions sharing an embedder (and, for chat, a retrieval
    # preset); without a cap that is proportional to the group count and can
    # exhaust the Milvus/embedder pools from one request. Small fan-outs (the
    # common case) stay fully parallel — the cap only engages past it.
    max_partition_concurrency: int = Field(default=16, gt=0)


# ---------------------------------------------------------------------------
# RAG
# ---------------------------------------------------------------------------


#: What the deprecated ``rag.mode`` (``RAG_MODE``) said, as ``rag.query_rewrite``.
_QUERY_REWRITE_BY_MODE = {"ChatBotRag": True, "SimpleRag": False}


class RAGConfig(ConfigMixin):
    #: The ``rag_mode`` of a chat request that names none (``metadata.rag_mode``).
    default_mode: Literal["classic", "agentic"] = "classic"
    #: ``classic``: the LLM rewrites the conversation into search queries, or
    #: (false) the latest user message is searched as it is.
    query_rewrite: bool = True
    chat_history_depth: int = 4
    max_contextualized_query_len: int = 512

    @model_validator(mode="before")
    @classmethod
    def _query_rewrite_from_mode(cls, data: Any) -> Any:
        """``rag.mode`` (``RAG_MODE``), deprecated: ``ChatBotRag`` is ``query_rewrite: true``, ``SimpleRag`` false.

        It wins over ``query_rewrite``, so a deployment that sets it keeps the
        behaviour it chose, whatever the shipped config says.
        """
        if not isinstance(data, dict) or "mode" not in data:
            return data
        data = dict(data)
        mode = data.pop("mode")
        if mode is None:
            return data
        if mode not in _QUERY_REWRITE_BY_MODE:
            raise ValueError(
                f"rag.mode (RAG_MODE) must be ChatBotRag or SimpleRag, not {mode!r}. It is deprecated: "
                "set rag.query_rewrite (RAG_QUERY_REWRITE) instead."
            )
        data["query_rewrite"] = _QUERY_REWRITE_BY_MODE[mode]
        _warn_deprecated_mode(mode)
        return data


@functools.cache
def _warn_deprecated_mode(mode: str) -> None:
    """Warn once per process: the config is loaded more than once."""
    rewrite = str(_QUERY_REWRITE_BY_MODE[mode]).lower()
    logger.warning(
        "rag.mode (RAG_MODE) is deprecated: %s sets rag.query_rewrite to %s, over any value given there. "
        "Replace it with RAG_QUERY_REWRITE=%s.",
        mode,
        rewrite,
        rewrite,
    )


# ---------------------------------------------------------------------------
# Map-Reduce
# ---------------------------------------------------------------------------


class MapReduceConfig(ConfigMixin):
    initial_batch_size: int = 10
    expansion_batch_size: int = 5
    max_total_documents: int = 20
    debug: bool = False


# ---------------------------------------------------------------------------
# WebSearch
# ---------------------------------------------------------------------------


class _BaseWebSearchConfig(ConfigMixin):
    base_url: str
    api_token: str = Field(default="", repr=False)
    top_k: int = 5
    lang: str = "fr-FR"
    max_tokens: int = 2000
    fetch_content: bool = True
    fetch_max_results: int = 3
    fetch_timeout: float = 1.0
    fetch_max_tokens: int = 500
    fetch_verify_ssl: bool = True


class StaanWebSearchConfig(_BaseWebSearchConfig):
    provider: Literal["staan"] = "staan"
    base_url: str = "https://api.staan.ai/search/web"


WebSearchConfig = Annotated[
    StaanWebSearchConfig,
    Field(discriminator="provider"),
]
