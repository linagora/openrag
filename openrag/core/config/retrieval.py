"""Retrieval, reranker, RAG mode, map-reduce, and web search configuration."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from .base import ConfigMixin

# ---------------------------------------------------------------------------
# Reranker
# ---------------------------------------------------------------------------


class _BaseRerankerConfig(ConfigMixin):
    model_name: str = "Alibaba-NLP/gte-multilingual-reranker-base"
    top_k: int = 10
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


class _BaseRetrieverConfig(ConfigMixin):
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
    # Upper bound on how many per-partition retrievals run concurrently within a
    # single request. A multi-partition search (notably a SUPER_ADMIN_MODE
    # ``openrag-all`` that expands to every partition) fans out one embed+search
    # per partition; without a cap that is proportional to the partition count
    # and can exhaust the Milvus/embedder pools from one request. Small fan-outs
    # (the common case) stay fully parallel — the cap only engages past it.
    max_partition_concurrency: int = Field(default=16, gt=0)


class SingleRetrieverConfig(_BaseRetrieverConfig):
    type: Literal["single"] = "single"


class MultiQueryRetrieverConfig(_BaseRetrieverConfig):
    type: Literal["multiQuery"] = "multiQuery"
    k_queries: int = 3


class HydeRetrieverConfig(_BaseRetrieverConfig):
    type: Literal["hyde"] = "hyde"
    combine: bool = False


RetrieverConfig = Annotated[
    SingleRetrieverConfig | MultiQueryRetrieverConfig | HydeRetrieverConfig,
    Field(discriminator="type"),
]


# ---------------------------------------------------------------------------
# RAG
# ---------------------------------------------------------------------------


class RAGConfig(ConfigMixin):
    mode: str = "ChatBotRag"
    chat_history_depth: int = 4
    max_contextualized_query_len: int = 512

    #: Allow a caller to ask for the exact context the LLM was given, by sending
    #: ``metadata: {"include_context": true}``. Off by default: the payload is
    #: the full retrieved text, which is far larger than a normal completion and
    #: is only wanted by offline evaluation. The request flag alone is not
    #: enough — an operator must turn this on for the deployment.
    #:
    #: Retrieval quality cannot otherwise be measured from the API: the
    #: ``sources`` field is filtered down to the chunks the model *cited*, and
    #: ``GET /search`` skips the reranker and the multi-query RRF fusion, so it
    #: cannot see a reranker change at all.
    expose_context: bool = False


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
