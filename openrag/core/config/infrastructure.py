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
    # Per-request timeout (s) applied to the Milvus sync and async clients.
    timeout: float = Field(default=120.0, gt=0)
    schema_version: int = 2


# ---------------------------------------------------------------------------
# RDB (Postgres)
# ---------------------------------------------------------------------------


class RDBConfig(ConfigMixin):
    host: str = "rdb"
    port: int = 5432
    user: str = "root"
    password: str = Field(default="", repr=False)
    default_file_quota: int = -1
    # `database` is intentionally optional — historically the database name is
    # derived from the Milvus collection name (`partitions_for_collection_{collection}`)
    # by the caller wiring the catalog store. The connection manager raises if
    # this is still None at initialize() time.
    database: str | None = None
    auto_create_database: bool = True
    run_migrations: bool = True
    pool_min_size: int = 5
    pool_max_size: int = 20
    command_timeout: int = 30


# ---------------------------------------------------------------------------
# Ray — worker pool, serve config
# ---------------------------------------------------------------------------


class RayIndexerConfig(ConfigMixin):
    # Indexing capacity = pool_size (worker actors) × max_tasks_per_worker (files per worker).
    pool_size: int = Field(default=1, ge=1)
    max_tasks_per_worker: int = Field(default=50, ge=1)


class RayServeConfig(ConfigMixin):
    enable: bool = False
    num_replicas: int = 1
    host: str = "0.0.0.0"
    port: int = 8080
    chainlit_port: int = 8090


class RayConfig(ConfigMixin):
    indexer: RayIndexerConfig = Field(default_factory=RayIndexerConfig)
    serve: RayServeConfig = Field(default_factory=RayServeConfig)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


# Bundled prompt templates: openrag/prompts/templates/ (this file is at
# openrag/core/config/infrastructure.py, three levels under the package root).
_DEFAULT_PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts" / "templates"


class PathsConfig(ConfigMixin):
    prompts_dir: Path = _DEFAULT_PROMPTS_DIR
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
    topic_tagger: str = "topic_tagger_tmpl.txt"
