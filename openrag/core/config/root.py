"""Root configuration — composes all sub-models into a single Settings object."""

from __future__ import annotations

from core.models.preset import PartitionConfig
from pydantic import Field

from .base import ConfigMixin
from .chunking import ChunkerConfig
from .endpoints import (
    EmbedderConfig,
    LLMConfig,
    LLMContextConfig,
    SemaphoreConfig,
    VLMConfig,
)
from .evaluation import EvaluationConfig
from .indexation import LoaderConfig
from .infrastructure import (
    PathsConfig,
    PromptsConfig,
    RayConfig,
    RDBConfig,
    ServerConfig,
    VectorDBConfig,
    VerboseConfig,
)
from .mcp import MCPServerConfig
from .model_endpoints import ModelsConfig
from .presets import PresetsConfig
from .retrieval import (
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
    mcp: MCPServerConfig = Field(default_factory=MCPServerConfig)
    evaluation: EvaluationConfig = Field(default_factory=EvaluationConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    presets: PresetsConfig = Field(default_factory=PresetsConfig)
    partitions: dict[str, PartitionConfig] = Field(default_factory=dict)

    def resolved_rdb(self) -> RDBConfig:
        """``rdb`` with its database name filled in.

        ``rdb.database`` is optional: historically the name is derived from the
        Milvus collection. Any process opening its own Postgres connection —
        the API's catalog store, or a Ray worker such as ``EvalRunner`` — must
        resolve it the same way, so the derivation lives here rather than in
        the callers.
        """
        if self.rdb.database is not None:
            return self.rdb
        return self.rdb.model_copy(
            update={"database": f"partitions_for_collection_{self.vectordb.collection_name}"},
        )
