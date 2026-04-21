"""Root configuration — composes all sub-models into a single Settings object."""

from __future__ import annotations

from pydantic import Field

from openrag.core.config.base import ConfigMixin
from openrag.core.config.chunking import ChunkerConfig
from openrag.core.config.endpoints import (
    EmbedderConfig,
    LLMConfig,
    LLMContextConfig,
    SemaphoreConfig,
    VLMConfig,
)
from openrag.core.config.indexation import LoaderConfig
from openrag.core.config.infrastructure import (
    PathsConfig,
    PromptsConfig,
    RayConfig,
    RDBConfig,
    ServerConfig,
    VectorDBConfig,
    VerboseConfig,
)
from openrag.core.config.retrieval import (
    MapReduceConfig,
    RAGConfig,
    RerankerConfig,
    RetrieverConfig,
    SingleRetrieverConfig,
    StaanWebSearchConfig,
    WebSearchConfig,
    _default_reranker_config,
)


class Settings(ConfigMixin):
    """Root configuration.

    Defaults here are fallbacks only. In production, values come from
    conf/config.yaml merged with environment variable overrides.
    """

    llm: LLMConfig = Field(default_factory=LLMConfig)
    vlm: VLMConfig = Field(default_factory=VLMConfig)
    semaphore: SemaphoreConfig = Field(default_factory=SemaphoreConfig)
    embedder: EmbedderConfig = Field(default_factory=EmbedderConfig)
    vectordb: VectorDBConfig = Field(default_factory=VectorDBConfig)
    rdb: RDBConfig = Field(default_factory=RDBConfig)
    reranker: RerankerConfig = Field(default_factory=_default_reranker_config)
    map_reduce: MapReduceConfig = Field(default_factory=MapReduceConfig)
    verbose: VerboseConfig = Field(default_factory=VerboseConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    llm_context: LLMContextConfig = Field(default_factory=LLMContextConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    prompts: PromptsConfig = Field(default_factory=PromptsConfig)
    loader: LoaderConfig = Field(default_factory=LoaderConfig)
    ray: RayConfig = Field(default_factory=RayConfig)
    chunker: ChunkerConfig = Field(default_factory=ChunkerConfig)
    retriever: RetrieverConfig = Field(default_factory=SingleRetrieverConfig)
    rag: RAGConfig = Field(default_factory=RAGConfig)
    websearch: WebSearchConfig = Field(default_factory=StaanWebSearchConfig)
