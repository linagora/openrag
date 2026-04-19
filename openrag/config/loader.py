"""Configuration loader — reads YAML defaults, merges env var overrides, validates with Pydantic."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml

from .models import Settings

logger = logging.getLogger(__name__)

_DEFAULT_CONF_DIR = Path(__file__).resolve().parent.parent.parent / "conf"

# ---------------------------------------------------------------------------
# Env var mappings: {env_var_name: dotted.config.path}
#
# Only values that should be overridable at deploy time are listed here.
# Secrets (API keys, passwords) and deployment-specific values (hosts, ports).
# Operational knobs that ops teams commonly tune are also included.
# ---------------------------------------------------------------------------
_ENV_OVERRIDES: list[tuple[str, str, type]] = [
    # LLM
    ("BASE_URL", "llm.base_url", str),
    ("MODEL", "llm.model", str),
    ("API_KEY", "llm.api_key", str),
    # VLM
    ("VLM_BASE_URL", "vlm.base_url", str),
    ("VLM_MODEL", "vlm.model", str),
    ("VLM_API_KEY", "vlm.api_key", str),
    # Semaphore
    ("LLM_SEMAPHORE", "semaphore.llm_semaphore", int),
    ("VLM_SEMAPHORE", "semaphore.vlm_semaphore", int),
    # Embedder
    ("EMBEDDER_MODEL_NAME", "embedder.model_name", str),
    ("EMBEDDER_BASE_URL", "embedder.base_url", str),
    ("EMBEDDER_API_KEY", "embedder.api_key", str),
    ("MAX_MODEL_LEN", "embedder.max_model_len", int),
    # VectorDB
    ("VDB_HOST", "vectordb.host", str),
    ("VDB_iPORT", "vectordb.port", int),  # legacy typo, kept for backward compat
    ("VDB_PORT", "vectordb.port", int),  # canonical name, wins if both are set
    ("VDB_CONNECTOR_NAME", "vectordb.connector_name", str),
    ("VDB_COLLECTION_NAME", "vectordb.collection_name", str),
    ("VDB_HYBRID_SEARCH", "vectordb.hybrid_search", bool),
    ("VDB_ENABLE_INSERTION", "vectordb.enable", bool),
    # RDB (Postgres)
    ("POSTGRES_HOST", "rdb.host", str),
    ("POSTGRES_PORT", "rdb.port", int),
    ("POSTGRES_USER", "rdb.user", str),
    ("POSTGRES_PASSWORD", "rdb.password", str),
    ("DEFAULT_FILE_QUOTA", "rdb.default_file_quota", int),
    # Reranker
    ("RERANKER_PROVIDER", "reranker.provider", str),
    ("RERANKER_ENABLED", "reranker.enabled", bool),
    ("RERANKER_MODEL", "reranker.model_name", str),
    ("RERANKER_TOP_K", "reranker.top_k", int),
    ("RERANKER_BASE_URL", "reranker.base_url", str),
    ("RERANKER_API_KEY", "reranker.api_key", str),
    ("RERANKER_TIMEOUT", "reranker.timeout", float),
    ("RERANKER_SEMAPHORE", "reranker.semaphore", int),
    # Map-Reduce
    ("MAP_REDUCE_INITIAL_BATCH_SIZE", "map_reduce.initial_batch_size", int),
    ("MAP_REDUCE_EXPANSION_BATCH_SIZE", "map_reduce.expansion_batch_size", int),
    ("MAP_REDUCE_MAX_TOTAL_DOCUMENTS", "map_reduce.max_total_documents", int),
    ("MAP_REDUCE_DEBUG", "map_reduce.debug", bool),
    # Verbose
    ("LOG_LEVEL", "verbose.level", str),
    # Server
    ("PREFERRED_URL_SCHEME", "server.preferred_url_scheme", str),
    # LLM Context
    ("MAX_LLM_CONTEXT_SIZE", "llm_context.max_llm_context_size", int),
    ("MAX_OUTPUT_TOKENS", "llm_context.max_output_tokens", int),
    # Paths
    ("PROMPTS_DIR", "paths.prompts_dir", str),
    ("DATA_DIR", "paths.data_dir", str),
    ("DB_DIR", "paths.db_dir", str),
    ("LOG_DIR", "paths.log_dir", str),
    # Loader
    ("IMAGE_CAPTIONING", "loader.image_captioning", bool),
    ("IMAGE_CAPTIONING_URL", "loader.image_captioning_url", bool),
    ("SAVE_MARKDOWN", "loader.save_markdown", bool),
    ("PDFLoader", "loader.file_loaders.pdf", str),
    ("AUDIOLOADER", "loader.file_loaders.wav", str),
    ("MARKER_MAX_TASKS_PER_CHILD", "loader.marker_max_tasks_per_child", int),
    ("MARKER_POOL_SIZE", "loader.marker_pool_size", int),
    ("MARKER_MAX_PROCESSES", "loader.marker_max_processes", int),
    ("MARKER_NUM_GPUS", "loader.marker_num_gpus", float),
    ("MARKER_TIMEOUT", "loader.marker_timeout", int),
    ("MARKER_PDFTEXT_WORKERS", "loader.marker_pdftext_workers", int),
    ("MARKER_CHUNK_SIZE", "loader.marker_chunk_size", int),
    ("DOCLING_NUM_GPUS", "loader.docling_num_gpus", float),
    ("DOCLING_POOL_SIZE", "loader.docling_pool_size", int),
    ("DOCLING_MAX_TASKS_PER_WORKER", "loader.docling_max_tasks_per_worker", int),
    ("WHISPER_MODEL", "loader.local_whisper.model", str),
    ("WHISPER_N_WORKERS", "loader.local_whisper.whisper_n_workers", int),
    ("WHISPER_NUM_GPUS", "loader.local_whisper.whisper_num_gpus", float),
    ("WHISPER_CONCURRENCY_PER_WORKER", "loader.local_whisper.whisper_concurrency_per_worker", int),
    ("TRANSCRIBER_BASE_URL", "loader.transcriber.base_url", str),
    ("TRANSCRIBER_API_KEY", "loader.transcriber.api_key", str),
    ("TRANSCRIBER_MODEL", "loader.transcriber.model_name", str),
    ("TRANSCRIBER_TIMEOUT", "loader.transcriber.timeout", int),
    ("TRANSCRIBER_MAX_CONCURRENT_CHUNKS", "loader.transcriber.max_concurrent_chunks", int),
    ("USE_WHISPER_LANG_DETECTOR", "loader.transcriber.use_whisper_lang_detector", bool),
    ("OPENAI_LOADER_BASE_URL", "loader.openai.base_url", str),
    ("OPENAI_LOADER_API_KEY", "loader.openai.api_key", str),
    ("OPENAI_LOADER_MODEL", "loader.openai.model", str),
    ("OPENAI_LOADER_TEMPERATURE", "loader.openai.temperature", float),
    ("OPENAI_LOADER_TIMEOUT", "loader.openai.timeout", int),
    ("OPENAI_LOADER_MAX_RETRIES", "loader.openai.max_retries", int),
    ("OPENAI_LOADER_TOP_P", "loader.openai.top_p", float),
    ("OPENAI_LOADER_CONCURRENCY_LIMIT", "loader.openai.concurrency_limit", int),
    # Ray
    ("RAY_NUM_GPUS", "ray.num_gpus", float),
    ("RAY_POOL_SIZE", "ray.pool_size", int),
    ("RAY_MAX_TASKS_PER_WORKER", "ray.max_tasks_per_worker", int),
    ("RAY_MAX_TASK_RETRIES", "ray.indexer.max_task_retries", int),
    ("INDEXER_SERIALIZE_TIMEOUT", "ray.indexer.serialize_timeout", int),
    ("VECTORDB_TIMEOUT", "ray.indexer.vectordb_timeout", int),
    ("INDEXER_DEFAULT_CONCURRENCY", "ray.indexer.concurrency_groups.default", int),
    ("INDEXER_UPDATE_CONCURRENCY", "ray.indexer.concurrency_groups.update", int),
    ("INDEXER_SEARCH_CONCURRENCY", "ray.indexer.concurrency_groups.search", int),
    ("INDEXER_DELETE_CONCURRENCY", "ray.indexer.concurrency_groups.delete", int),
    ("INDEXER_SERIALIZE_CONCURRENCY", "ray.indexer.concurrency_groups.serialize", int),
    ("INDEXER_CHUNK_CONCURRENCY", "ray.indexer.concurrency_groups.chunk", int),
    ("INDEXER_INSERT_CONCURRENCY", "ray.indexer.concurrency_groups.insert", int),
    ("RAY_SEMAPHORE_CONCURRENCY", "ray.semaphore.concurrency", int),
    ("ENABLE_RAY_SERVE", "ray.serve.enable", bool),
    ("RAY_SERVE_NUM_REPLICAS", "ray.serve.num_replicas", int),
    ("RAY_SERVE_HOST", "ray.serve.host", str),
    ("RAY_SERVE_PORT", "ray.serve.port", int),
    ("CHAINLIT_PORT", "ray.serve.chainlit_port", int),
    # Chunker
    ("CHUNKER", "chunker.name", str),
    ("CONTEXTUAL_RETRIEVAL", "chunker.contextual_retrieval", bool),
    ("CONTEXTUALIZATION_TIMEOUT", "chunker.contextualization_timeout", int),
    ("MAX_CONCURRENT_CONTEXTUALIZATION", "chunker.max_concurrent_contextualization", int),
    ("CHUNK_SIZE", "chunker.chunk_size", int),
    ("CHUNK_OVERLAP_RATE", "chunker.chunk_overlap_rate", float),
    # Retriever
    ("RETRIEVER_TYPE", "retriever.type", str),
    ("RETRIEVER_TOP_K", "retriever.top_k", int),
    ("SIMILARITY_THRESHOLD", "retriever.similarity_threshold", float),
    ("WITH_SURROUNDING_CHUNKS", "retriever.with_surrounding_chunks", bool),
    ("INCLUDE_RELATED", "retriever.include_related", bool),
    ("INCLUDE_ANCESTORS", "retriever.include_ancestors", bool),
    ("RELATED_LIMIT", "retriever.related_limit", int),
    ("MAX_DEPTH", "retriever.max_ancestor_depth", int),
    ("RETRIEVER_ALLOW_FILTERLESS_FALLBACK", "retriever.allow_filterless_fallback", bool),
    # RAG
    ("RAG_MODE", "rag.mode", str),
    # WebSearch
    ("WEBSEARCH_PROVIDER", "websearch.provider", str),
    ("WEBSEARCH_API_TOKEN", "websearch.api_token", str),
    ("WEBSEARCH_BASE_URL", "websearch.base_url", str),
    ("WEBSEARCH_TOP_K", "websearch.top_k", int),
    ("WEBSEARCH_LANG", "websearch.lang", str),
    ("WEBSEARCH_MAX_TOKENS", "websearch.max_tokens", int),
    ("WEBSEARCH_FETCH_CONTENT", "websearch.fetch_content", bool),
    ("WEBSEARCH_FETCH_MAX_RESULTS", "websearch.fetch_max_results", int),
    ("WEBSEARCH_FETCH_TIMEOUT", "websearch.fetch_timeout", float),
    ("WEBSEARCH_FETCH_MAX_TOKENS", "websearch.fetch_max_tokens", int),
    ("WEBSEARCH_FETCH_VERIFY_SSL", "websearch.fetch_verify_ssl", bool),
]

# Audio loader env var applies to all audio/video extensions
_AUDIO_EXTENSIONS = ("mp3", "flac", "ogg", "aac", "flv", "wma", "mp4")


def _load_yaml(path: Path) -> dict[str, Any]:
    """Load a YAML file, returning empty dict if not found."""
    if not path.exists():
        logger.warning("Config file not found: %s — using defaults", path)
        return {}
    with open(path) as f:
        data = yaml.safe_load(f)
    return data or {}


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base."""
    merged = base.copy()
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _set_nested(data: dict, dotted_path: str, value: Any) -> None:
    """Set a value in a nested dict using a dotted path like 'ray.indexer.timeout'."""
    keys = dotted_path.split(".")
    current = data
    for key in keys[:-1]:
        current = current.setdefault(key, {})
    current[keys[-1]] = value


def _coerce(value: str, target_type: type, env_var: str = "") -> Any:
    """Coerce a string env var value to the target type."""
    if target_type is bool:
        lower = value.lower()
        if lower in ("true", "1", "yes"):
            return True
        if lower in ("false", "0", "no"):
            return False
        raise ValueError(f"Invalid value for {env_var}: expected bool, got {value!r}")
    try:
        if target_type is int:
            return int(value)
        if target_type is float:
            return float(value)
    except ValueError:
        raise ValueError(f"Invalid value for {env_var}: expected {target_type.__name__}, got {value!r}")
    return value


def _apply_env_overrides(data: dict) -> dict:
    """Apply environment variable overrides to the config dict."""
    for env_var, dotted_path, target_type in _ENV_OVERRIDES:
        value = os.environ.get(env_var)
        if value is not None and value != "":
            _set_nested(data, dotted_path, _coerce(value, target_type, env_var))

    # SEMAPHORE sets both LLM and VLM semaphores (convenience shorthand)
    semaphore = os.environ.get("SEMAPHORE")
    if semaphore:
        sem_value = _coerce(semaphore, int, "SEMAPHORE")
        sem = data.setdefault("semaphore", {})
        sem.setdefault("llm_semaphore", sem_value)
        sem.setdefault("vlm_semaphore", sem_value)

    # AUDIOLOADER applies to all audio/video extensions
    audio_loader = os.environ.get("AUDIOLOADER")
    if audio_loader:
        file_loaders = data.setdefault("loader", {}).setdefault("file_loaders", {})
        for ext in _AUDIO_EXTENSIONS:
            file_loaders[ext] = audio_loader

    return data


def load_config(
    conf_dir: Path | str | None = None,
    overrides: dict[str, Any] | None = None,
) -> Settings:
    """Load configuration: YAML defaults → env var overrides → Pydantic validation.

    Args:
        conf_dir: Path to the configuration directory. Defaults to ``conf/``
                  at the project root, overridable via ``OPENRAG_CONF_DIR``.
        overrides: Programmatic overrides (useful for tests).
    """
    from dotenv import load_dotenv

    load_dotenv()

    env_conf_dir = os.environ.get("OPENRAG_CONF_DIR")
    if conf_dir:
        conf_dir = Path(conf_dir)
    elif env_conf_dir:
        conf_dir = Path(env_conf_dir)
    else:
        conf_dir = _DEFAULT_CONF_DIR

    # 1. Load YAML defaults
    data = _load_yaml(conf_dir / "config.yaml")

    # Remove YAML anchors (keys starting with _) — they are DRY helpers, not config sections
    data = {k: v for k, v in data.items() if not k.startswith("_")}

    # 2. Apply env var overrides
    data = _apply_env_overrides(data)

    # Strip blank reranker.base_url so the provider-specific Pydantic default applies
    reranker = data.get("reranker")
    if isinstance(reranker, dict) and not reranker.get("base_url"):
        reranker.pop("base_url", None)

    # 3. Apply programmatic overrides (tests)
    if overrides:
        data = _deep_merge(data, overrides)

    # 4. Resolve paths (after all merging so overrides are honored)
    paths = data.get("paths", {})
    for key in ("prompts_dir", "data_dir", "db_dir", "log_dir"):
        if key in paths and paths[key]:
            paths[key] = str(Path(paths[key]).resolve())

    # 5. Validate with Pydantic
    return Settings(**data)
