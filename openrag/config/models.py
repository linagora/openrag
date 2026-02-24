"""Pydantic config models for each configuration section.

Each model corresponds to a section in the old .hydra_config/config.yaml.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import Field

from .mixins import ConfigMixin, _env, _env_bool, _env_float, _env_int


# ---------------------------------------------------------------------------
# LLM params (shared between llm and vlm)
# ---------------------------------------------------------------------------
class LLMParamsConfig(ConfigMixin):
    temperature: float = 0.1
    timeout: int = 60
    max_retries: int = 2
    logprobs: bool = True


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------
class LLMConfig(ConfigMixin):
    temperature: float = 0.1
    timeout: int = 60
    max_retries: int = 2
    logprobs: bool = True
    base_url: str = ""
    model: str = ""
    api_key: str = ""

    @classmethod
    def from_env(cls) -> LLMConfig:
        return cls(
            base_url=_env("BASE_URL", ""),
            model=_env("MODEL", ""),
            api_key=_env("API_KEY", ""),
        )


# ---------------------------------------------------------------------------
# VLM
# ---------------------------------------------------------------------------
class VLMConfig(ConfigMixin):
    temperature: float = 0.1
    timeout: int = 60
    max_retries: int = 2
    logprobs: bool = True
    base_url: str = ""
    model: str = ""
    api_key: str = ""

    @classmethod
    def from_env(cls) -> VLMConfig:
        return cls(
            base_url=_env("VLM_BASE_URL", ""),
            model=_env("VLM_MODEL", ""),
            api_key=_env("VLM_API_KEY", ""),
        )


# ---------------------------------------------------------------------------
# Semaphore
# ---------------------------------------------------------------------------
class SemaphoreConfig(ConfigMixin):
    llm_semaphore: int = 10
    vlm_semaphore: int = 10

    @classmethod
    def from_env(cls) -> SemaphoreConfig:
        return cls(
            llm_semaphore=_env_int("LLM_SEMAPHORE", 10),
            vlm_semaphore=_env_int("VLM_SEMAPHORE", 10),
        )


# ---------------------------------------------------------------------------
# Embedder
# ---------------------------------------------------------------------------
class EmbedderConfig(ConfigMixin):
    provider: str = "openai"
    model_name: str = "jinaai/jina-embeddings-v3"
    base_url: str = "http://vllm:8000/v1"
    api_key: str = "EMPTY"
    max_model_len: int = 8192

    @classmethod
    def from_env(cls) -> EmbedderConfig:
        return cls(
            model_name=_env("EMBEDDER_MODEL_NAME", "jinaai/jina-embeddings-v3"),
            base_url=_env("EMBEDDER_BASE_URL", "http://vllm:8000/v1"),
            api_key=_env("EMBEDDER_API_KEY", "EMPTY"),
            max_model_len=_env_int("MAX_MODEL_LEN", 8192),
        )


# ---------------------------------------------------------------------------
# VectorDB
# ---------------------------------------------------------------------------
class VectorDBConfig(ConfigMixin):
    host: str = "milvus"
    port: str = "19530"
    connector_name: str = "milvus"
    collection_name: str = "vdb_test"
    hybrid_search: bool = True
    enable: bool = True

    @classmethod
    def from_env(cls) -> VectorDBConfig:
        return cls(
            host=_env("VDB_HOST", "milvus"),
            port=_env("VDB_iPORT", "19530"),
            connector_name=_env("VDB_CONNECTOR_NAME", "milvus"),
            collection_name=_env("VDB_COLLECTION_NAME", "vdb_test"),
            hybrid_search=_env_bool("VDB_HYBRID_SEARCH", True),
        )


# ---------------------------------------------------------------------------
# RDB (Postgres)
# ---------------------------------------------------------------------------
class RDBConfig(ConfigMixin):
    host: str = "rdb"
    port: str = "5432"
    user: str = "root"
    password: str = "root_password"
    default_file_quota: int = -1

    @classmethod
    def from_env(cls) -> RDBConfig:
        return cls(
            host=_env("POSTGRES_HOST", "rdb"),
            port=_env("POSTGRES_PORT", "5432"),
            user=_env("POSTGRES_USER", "root"),
            password=_env("POSTGRES_PASSWORD", "root_password"),
            default_file_quota=_env_int("DEFAULT_FILE_QUOTA", -1),
        )


# ---------------------------------------------------------------------------
# Reranker
# ---------------------------------------------------------------------------
class RerankerConfig(ConfigMixin):
    enable: bool = True
    model_name: str = "Alibaba-NLP/gte-multilingual-reranker-base"
    top_k: int = 10
    base_url: str = ""

    @classmethod
    def from_env(cls) -> RerankerConfig:
        enable = _env_bool("RERANKER_ENABLED", True)
        model_name = _env("RERANKER_MODEL", "Alibaba-NLP/gte-multilingual-reranker-base")
        top_k = _env_int("RERANKER_TOP_K", 10)

        # base_url default was: http://reranker:${RERANKER_PORT, 7997}
        base_url = _env("RERANKER_BASE_URL")
        if not base_url:
            port = _env("RERANKER_PORT", "7997")
            base_url = f"http://reranker:{port}"

        return cls(
            enable=enable,
            model_name=model_name,
            top_k=top_k,
            base_url=base_url,
        )


# ---------------------------------------------------------------------------
# MapReduce
# ---------------------------------------------------------------------------
class MapReduceConfig(ConfigMixin):
    initial_batch_size: int = 10
    expansion_batch_size: int = 5
    max_total_documents: int = 20
    debug: bool = False

    @classmethod
    def from_env(cls) -> MapReduceConfig:
        return cls(
            initial_batch_size=_env_int("MAP_REDUCE_INITIAL_BATCH_SIZE", 10),
            expansion_batch_size=_env_int("MAP_REDUCE_EXPANSION_BATCH_SIZE", 5),
            max_total_documents=_env_int("MAP_REDUCE_MAX_TOTAL_DOCUMENTS", 20),
            debug=_env_bool("MAP_REDUCE_DEBUG", False),
        )


# ---------------------------------------------------------------------------
# Verbose
# ---------------------------------------------------------------------------
class VerboseConfig(ConfigMixin):
    level: str = "DEBUG"

    @classmethod
    def from_env(cls) -> VerboseConfig:
        return cls(level=_env("LOG_LEVEL", "DEBUG"))


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------
class ServerConfig(ConfigMixin):
    preferred_url_scheme: str | None = None

    @classmethod
    def from_env(cls) -> ServerConfig:
        val = _env("PREFERRED_URL_SCHEME")
        if val and val.lower() == "null":
            val = None
        return cls(preferred_url_scheme=val)


# ---------------------------------------------------------------------------
# LLM Context
# ---------------------------------------------------------------------------
class LLMContextConfig(ConfigMixin):
    max_llm_context_size: int = 8192
    max_output_tokens: int = 1024

    @classmethod
    def from_env(cls) -> LLMContextConfig:
        return cls(
            max_llm_context_size=_env_int("MAX_LLM_CONTEXT_SIZE", 8192),
            max_output_tokens=_env_int("MAX_OUTPUT_TOKENS", 1024),
        )


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
class PathsConfig(ConfigMixin):
    prompts_dir: Path = Path("../prompts/example1")
    data_dir: Path = Path("../data")
    db_dir: Path = Path("/app/db")
    log_dir: Path = Path("/app/logs")

    model_config = {"arbitrary_types_allowed": True}

    @classmethod
    def from_env(cls) -> PathsConfig:
        return cls(
            prompts_dir=Path(_env("PROMPTS_DIR", "../prompts/example1")).resolve(),
            data_dir=Path(_env("DATA_DIR", "../data")).resolve(),
            db_dir=Path(_env("DB_DIR", "/app/db")),
            log_dir=Path(_env("LOG_DIR", "/app/logs")).resolve(),
        )


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------
class PromptsConfig(ConfigMixin):
    sys_prompt: str = "sys_prompt_tmpl.txt"
    query_contextualizer: str = "query_contextualizer_tmpl.txt"
    chunk_contextualizer: str = "chunk_contextualizer_tmpl.txt"
    image_describer: str = "image_captioning_tmpl.txt"
    spoken_style_answer: str = "spoken_style_answer_tmpl.txt"
    hyde: str = "hyde.txt"
    multi_query: str = "multi_query_pmpt_tmpl.txt"


# ---------------------------------------------------------------------------
# Transcriber (nested under loader)
# ---------------------------------------------------------------------------
class TranscriberConfig(ConfigMixin):
    base_url: str = "http://transcriber:8000/v1"
    api_key: str = "EMPTY"
    model_name: str = "openai/whisper-large-v3-turbo"
    max_chunk_ms: int = 30000
    silence_thresh_db: int = -40
    min_silence_len_ms: int = 500
    max_concurrent_chunks: int = 20
    timeout: int = 60

    @classmethod
    def from_env(cls) -> TranscriberConfig:
        return cls(
            base_url=_env("TRANSCRIBER_BASE_URL", "http://transcriber:8000/v1"),
            api_key=_env("TRANSCRIBER_API_KEY", "EMPTY"),
            model_name=_env("TRANSCRIBER_MODEL", "openai/whisper-large-v3-turbo"),
            max_chunk_ms=_env_int("TRANSCRIBER_MAX_CHUNK_MS", 30000),
            silence_thresh_db=_env_int("TRANSCRIBER_SILENCE_THRESH_DB", -40),
            min_silence_len_ms=_env_int("TRANSCRIBER_MIN_SILENCE_LEN_MS", 500),
            max_concurrent_chunks=_env_int("TRANSCRIBER_MAX_CONCURRENT_CHUNKS", 20),
            timeout=_env_int("TRANSCRIBER_TIMEOUT", 60),
        )


# ---------------------------------------------------------------------------
# OpenAI Loader (nested under loader)
# ---------------------------------------------------------------------------
class OpenAILoaderConfig(ConfigMixin):
    base_url: str = "http://openai:8000/v1"
    api_key: str = "EMPTY"
    model: str = "dotsocr-model"
    temperature: float = 0.2
    timeout: int = 180
    max_retries: int = 2
    top_p: float = 0.9
    concurrency_limit: int = 20

    @classmethod
    def from_env(cls) -> OpenAILoaderConfig:
        return cls(
            base_url=_env("OPENAI_LOADER_BASE_URL", "http://openai:8000/v1"),
            api_key=_env("OPENAI_LOADER_API_KEY", "EMPTY"),
            model=_env("OPENAI_LOADER_MODEL", "dotsocr-model"),
            temperature=_env_float("OPENAI_LOADER_TEMPERATURE", 0.2),
            timeout=_env_int("OPENAI_LOADER_TIMEOUT", 180),
            max_retries=_env_int("OPENAI_LOADER_MAX_RETRIES", 2),
            top_p=_env_float("OPENAI_LOADER_TOP_P", 0.9),
            concurrency_limit=_env_int("OPENAI_LOADER_CONCURRENCY_LIMIT", 20),
        )


# ---------------------------------------------------------------------------
# File loaders mapping (nested under loader)
# ---------------------------------------------------------------------------
class FileLoadersConfig(ConfigMixin):
    txt: str = "TextLoader"
    pdf: str = "MarkerLoader"
    eml: str = "EmlLoader"
    docx: str = "DocxLoader"
    pptx: str = "PPTXLoader"
    doc: str = "DocLoader"
    png: str = "ImageLoader"
    jpeg: str = "ImageLoader"
    jpg: str = "ImageLoader"
    svg: str = "ImageLoader"
    wav: str = "VideoAudioLoader"
    mp3: str = "VideoAudioLoader"
    mp4: str = "VideoAudioLoader"
    ogg: str = "VideoAudioLoader"
    flv: str = "VideoAudioLoader"
    wma: str = "VideoAudioLoader"
    aac: str = "VideoAudioLoader"
    md: str = "MarkdownLoader"

    @classmethod
    def from_env(cls) -> FileLoadersConfig:
        return cls(
            pdf=_env("PDFLoader", "MarkerLoader"),
        )


# ---------------------------------------------------------------------------
# Mimetypes mapping (nested under loader)
# ---------------------------------------------------------------------------
class MimetypesConfig(ConfigMixin):
    """Maps MIME type strings to file extensions."""

    _mapping: dict[str, str] = {
        "text/plain": ".txt",
        "text/markdown": ".md",
        "application/pdf": ".pdf",
        "message/rfc822": ".eml",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
        "application/msword": ".doc",
        "image/png": ".png",
        "image/jpeg": ".jpeg",
        "audio/vnd.wav": ".wav",
        "audio/mpeg": ".mp3",
    }

    def get(self, key: str, default: Any = None) -> Any:
        return self._mapping.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self._mapping[key]

    def keys(self):
        return self._mapping.keys()

    def values(self):
        return self._mapping.values()

    def items(self):
        return self._mapping.items()

    def __iter__(self):
        return iter(self._mapping)

    def __contains__(self, key: str) -> bool:
        return key in self._mapping


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------
class LoaderConfig(ConfigMixin):
    image_captioning: bool = True
    image_captioning_url: bool = True
    save_markdown: bool = False
    mimetypes: MimetypesConfig = Field(default_factory=MimetypesConfig)
    file_loaders: FileLoadersConfig = Field(default_factory=FileLoadersConfig)
    marker_max_tasks_per_child: int = 10
    marker_pool_size: int = 1
    marker_max_processes: int = 2
    marker_min_processes: int = 1
    marker_num_gpus: float = 0.01
    marker_timeout: int = 3600
    transcriber: TranscriberConfig = Field(default_factory=TranscriberConfig)
    openai: OpenAILoaderConfig = Field(default_factory=OpenAILoaderConfig)
    docling_num_gpus: float = 0.01
    docling_pool_size: int = 1
    docling_max_tasks_per_worker: int = 2

    @classmethod
    def from_env(cls) -> LoaderConfig:
        return cls(
            image_captioning=_env_bool("IMAGE_CAPTIONING", True),
            image_captioning_url=_env_bool("IMAGE_CAPTIONING_URL", True),
            save_markdown=_env_bool("SAVE_MARKDOWN", False),
            file_loaders=FileLoadersConfig.from_env(),
            marker_max_tasks_per_child=_env_int("MARKER_MAX_TASKS_PER_CHILD", 10),
            marker_pool_size=_env_int("MARKER_POOL_SIZE", 1),
            marker_max_processes=_env_int("MARKER_MAX_PROCESSES", 2),
            marker_min_processes=_env_int("MARKER_MIN_PROCESSES", 1),
            marker_num_gpus=_env_float("MARKER_NUM_GPUS", 0.01),
            marker_timeout=_env_int("MARKER_TIMEOUT", 3600),
            transcriber=TranscriberConfig.from_env(),
            openai=OpenAILoaderConfig.from_env(),
            docling_num_gpus=_env_float("DOCLING_NUM_GPUS", 0.01),
            docling_pool_size=_env_int("DOCLING_POOL_SIZE", 1),
            docling_max_tasks_per_worker=_env_int("DOCLING_MAX_TASKS_PER_WORKER", 2),
        )


# ---------------------------------------------------------------------------
# Ray — Indexer concurrency groups
# ---------------------------------------------------------------------------
class IndexerConcurrencyGroupsConfig(ConfigMixin):
    default: int = 1000
    update: int = 100
    search: int = 100
    delete: int = 100
    serialize: int = 50
    chunk: int = 1000
    insert: int = 100

    @classmethod
    def from_env(cls) -> IndexerConcurrencyGroupsConfig:
        return cls(
            default=_env_int("INDEXER_DEFAULT_CONCURRENCY", 1000),
            update=_env_int("INDEXER_UPDATE_CONCURRENCY", 100),
            search=_env_int("INDEXER_SEARCH_CONCURRENCY", 100),
            delete=_env_int("INDEXER_DELETE_CONCURRENCY", 100),
            serialize=_env_int("INDEXER_SERIALIZE_CONCURRENCY", 50),
            chunk=_env_int("INDEXER_CHUNK_CONCURRENCY", 1000),
            insert=_env_int("INDEXER_INSERT_CONCURRENCY", 100),
        )


class RayIndexerConfig(ConfigMixin):
    max_task_retries: int = 2
    serialize_timeout: int = 3600
    concurrency_groups: IndexerConcurrencyGroupsConfig = Field(
        default_factory=IndexerConcurrencyGroupsConfig,
    )

    @classmethod
    def from_env(cls) -> RayIndexerConfig:
        return cls(
            max_task_retries=_env_int("RAY_MAX_TASK_RETRIES", 2),
            serialize_timeout=_env_int("INDEXER_SERIALIZE_TIMEOUT", 3600),
            concurrency_groups=IndexerConcurrencyGroupsConfig.from_env(),
        )


class RaySemaphoreConfig(ConfigMixin):
    concurrency: int = 100000

    @classmethod
    def from_env(cls) -> RaySemaphoreConfig:
        return cls(concurrency=_env_int("RAY_SEMAPHORE_CONCURRENCY", 100000))


class RayServeConfig(ConfigMixin):
    enable: bool = False
    num_replicas: int = 1
    host: str = "0.0.0.0"
    port: str = "8080"
    chainlit_port: str = "8090"

    @classmethod
    def from_env(cls) -> RayServeConfig:
        return cls(
            enable=_env_bool("ENABLE_RAY_SERVE", False),
            num_replicas=_env_int("RAY_SERVE_NUM_REPLICAS", 1),
            host=_env("RAY_SERVE_HOST", "0.0.0.0"),
            port=_env("RAY_SERVE_PORT", "8080"),
            chainlit_port=_env("CHAINLIT_PORT", "8090"),
        )


class RayConfig(ConfigMixin):
    num_gpus: float = 0.01
    pool_size: int = 1
    max_tasks_per_worker: int = 8
    indexer: RayIndexerConfig = Field(default_factory=RayIndexerConfig)
    semaphore: RaySemaphoreConfig = Field(default_factory=RaySemaphoreConfig)
    serve: RayServeConfig = Field(default_factory=RayServeConfig)

    @classmethod
    def from_env(cls) -> RayConfig:
        return cls(
            num_gpus=_env_float("RAY_NUM_GPUS", 0.01),
            pool_size=_env_int("RAY_POOL_SIZE", 1),
            max_tasks_per_worker=_env_int("RAY_MAX_TASKS_PER_WORKER", 8),
            indexer=RayIndexerConfig.from_env(),
            semaphore=RaySemaphoreConfig.from_env(),
            serve=RayServeConfig.from_env(),
        )


# ---------------------------------------------------------------------------
# Chunker
# ---------------------------------------------------------------------------
class ChunkerConfig(ConfigMixin):
    name: str = "recursive_splitter"
    contextual_retrieval: bool = True
    contextualization_timeout: int = 120
    max_concurrent_contextualization: int = 10
    chunk_size: int = 512
    chunk_overlap_rate: float = 0.2

    @classmethod
    def from_env(cls) -> ChunkerConfig:
        return cls(
            name=_env("CHUNKER", "recursive_splitter"),
            contextual_retrieval=_env_bool("CONTEXTUAL_RETRIEVAL", True),
            contextualization_timeout=_env_int("CONTEXTUALIZATION_TIMEOUT", 120),
            max_concurrent_contextualization=_env_int("MAX_CONCURRENT_CONTEXTUALIZATION", 10),
            chunk_size=_env_int("CHUNK_SIZE", 512),
            chunk_overlap_rate=_env_float("CHUNK_OVERLAP_RATE", 0.2),
        )


# ---------------------------------------------------------------------------
# Retriever
# ---------------------------------------------------------------------------
class RetrieverConfig(ConfigMixin):
    type: str = "single"
    top_k: int = 50
    similarity_threshold: float = 0.6
    with_surrounding_chunks: bool = False
    include_related: bool = True
    include_ancestors: bool = True
    related_limit: int = 10
    max_ancestor_depth: int = 10
    # Extra params for specific retriever types
    k_queries: int = 3
    combine: bool = False

    @classmethod
    def from_env(cls) -> RetrieverConfig:
        return cls(
            type=_env("RETRIEVER_TYPE", "single"),
            top_k=_env_int("RETRIEVER_TOP_K", 50),
            similarity_threshold=_env_float("SIMILARITY_THRESHOLD", 0.6),
            with_surrounding_chunks=_env_bool("WITH_SURROUNDING_CHUNKS", False),
            include_related=_env_bool("INCLUDE_RELATED", True),
            include_ancestors=_env_bool("INCLUDE_ANCESTORS", True),
            related_limit=_env_int("RELATED_LIMIT", 10),
            max_ancestor_depth=_env_int("MAX_DEPTH", 10),
        )


# ---------------------------------------------------------------------------
# RAG
# ---------------------------------------------------------------------------
class RAGConfig(ConfigMixin):
    mode: str = "ChatBotRag"
    chat_history_depth: int = 4
    max_contextualized_query_len: int = 512

    @classmethod
    def from_env(cls) -> RAGConfig:
        return cls(
            mode=_env("RAG_MODE", "ChatBotRag"),
        )
