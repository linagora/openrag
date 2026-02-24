"""Root Settings model and cached singleton."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field

from .mixins import ConfigMixin
from .models import (
    ChunkerConfig,
    EmbedderConfig,
    LLMConfig,
    LLMContextConfig,
    LLMParamsConfig,
    LoaderConfig,
    MapReduceConfig,
    PathsConfig,
    PromptsConfig,
    RAGConfig,
    RayConfig,
    RDBConfig,
    RerankerConfig,
    RetrieverConfig,
    SemaphoreConfig,
    ServerConfig,
    VectorDBConfig,
    VerboseConfig,
    VLMConfig,
)


class Settings(ConfigMixin):
    """Root configuration — composes all sub-models."""

    llm_params: LLMParamsConfig = Field(default_factory=LLMParamsConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    vlm: VLMConfig = Field(default_factory=VLMConfig)
    semaphore: SemaphoreConfig = Field(default_factory=SemaphoreConfig)
    embedder: EmbedderConfig = Field(default_factory=EmbedderConfig)
    vectordb: VectorDBConfig = Field(default_factory=VectorDBConfig)
    rdb: RDBConfig = Field(default_factory=RDBConfig)
    reranker: RerankerConfig = Field(default_factory=RerankerConfig)
    map_reduce: MapReduceConfig = Field(default_factory=MapReduceConfig)
    verbose: VerboseConfig = Field(default_factory=VerboseConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    llm_context: LLMContextConfig = Field(default_factory=LLMContextConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    prompts: PromptsConfig = Field(default_factory=PromptsConfig)
    loader: LoaderConfig = Field(default_factory=LoaderConfig)
    ray: RayConfig = Field(default_factory=RayConfig)
    chunker: ChunkerConfig = Field(default_factory=ChunkerConfig)
    retriever: RetrieverConfig = Field(default_factory=RetrieverConfig)
    rag: RAGConfig = Field(default_factory=RAGConfig)

    @classmethod
    def from_env(cls) -> Settings:
        """Build the entire settings tree from environment variables."""
        return cls(
            llm=LLMConfig.from_env(),
            vlm=VLMConfig.from_env(),
            semaphore=SemaphoreConfig.from_env(),
            embedder=EmbedderConfig.from_env(),
            vectordb=VectorDBConfig.from_env(),
            rdb=RDBConfig.from_env(),
            reranker=RerankerConfig.from_env(),
            map_reduce=MapReduceConfig.from_env(),
            verbose=VerboseConfig.from_env(),
            server=ServerConfig.from_env(),
            llm_context=LLMContextConfig.from_env(),
            paths=PathsConfig.from_env(),
            loader=LoaderConfig.from_env(),
            ray=RayConfig.from_env(),
            chunker=ChunkerConfig.from_env(),
            retriever=RetrieverConfig.from_env(),
            rag=RAGConfig.from_env(),
        )


@lru_cache
def get_settings() -> Settings:
    """Cached singleton — one Settings instance per process."""
    from dotenv import load_dotenv

    load_dotenv()
    return Settings.from_env()
