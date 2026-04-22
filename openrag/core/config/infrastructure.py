"""Infrastructure configuration — VectorDB, Postgres, Ray, paths, server."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field

from .base import ConfigMixin

# ---------------------------------------------------------------------------
# VectorDB (Milvus)
# ---------------------------------------------------------------------------


class VectorDBConfig(ConfigMixin):
    host: str = "milvus"
    port: int = 19530
    connector_name: str = "milvus"
    collection_name: str = "vdb_test"
    hybrid_search: bool = True
    enable: bool = True
    schema_version: int = 1


# ---------------------------------------------------------------------------
# RDB (Postgres)
# ---------------------------------------------------------------------------


class RDBConfig(ConfigMixin):
    host: str = "rdb"
    port: int = 5432
    user: str = "root"
    password: str = Field(default="", repr=False)
    default_file_quota: int = -1


# ---------------------------------------------------------------------------
# Ray — concurrency groups, serve config
# ---------------------------------------------------------------------------


class IndexerConcurrencyGroupsConfig(ConfigMixin):
    default: int = 1000
    update: int = 100
    search: int = 100
    delete: int = 100
    serialize: int = 50
    chunk: int = 1000
    insert: int = 100


class RayIndexerConfig(ConfigMixin):
    max_task_retries: int = 2
    serialize_timeout: int = 3600
    vectordb_timeout: int = 30
    concurrency_groups: IndexerConcurrencyGroupsConfig = Field(
        default_factory=IndexerConcurrencyGroupsConfig,
    )


class RaySemaphoreConfig(ConfigMixin):
    concurrency: int = 100000


class RayServeConfig(ConfigMixin):
    enable: bool = False
    num_replicas: int = 1
    host: str = "0.0.0.0"
    port: int = 8080
    chainlit_port: int = 8090


class RayConfig(ConfigMixin):
    num_gpus: float = 0.01
    pool_size: int = 1
    max_tasks_per_worker: int = 8
    indexer: RayIndexerConfig = Field(default_factory=RayIndexerConfig)
    semaphore: RaySemaphoreConfig = Field(default_factory=RaySemaphoreConfig)
    serve: RayServeConfig = Field(default_factory=RayServeConfig)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


class PathsConfig(ConfigMixin):
    prompts_dir: Path = Path("../prompts/example1")
    data_dir: Path = Path("../data")
    db_dir: Path = Path("/app/db")
    log_dir: Path = Path("/app/logs")

    model_config = {**ConfigMixin.model_config, "arbitrary_types_allowed": True}


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


class ServerConfig(ConfigMixin):
    preferred_url_scheme: str | None = None


# ---------------------------------------------------------------------------
# Verbose / logging
# ---------------------------------------------------------------------------


class VerboseConfig(ConfigMixin):
    level: str = "DEBUG"


# ---------------------------------------------------------------------------
# Prompts (file name mapping)
# ---------------------------------------------------------------------------


class PromptsConfig(ConfigMixin):
    sys_prompt: str = "sys_prompt_tmpl.txt"
    query_contextualizer: str = "query_contextualizer_tmpl.txt"
    chunk_contextualizer: str = "chunk_contextualizer_tmpl.txt"
    image_describer: str = "image_captioning_tmpl.txt"
    spoken_style_answer: str = "spoken_style_answer_tmpl.txt"
    hyde: str = "hyde.txt"
    multi_query: str = "multi_query_pmpt_tmpl.txt"
