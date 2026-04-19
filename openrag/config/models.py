"""Pydantic config models — pure validation schemas.

Each model corresponds to a configuration section. Defaults are fallbacks only;
in production, values come from conf/config.yaml merged with env var overrides
(see loader.py for the merge logic).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Base mixin — frozen models with dict-like backward compat
# ---------------------------------------------------------------------------
class ConfigMixin(BaseModel):
    """Frozen Pydantic model with dict-like access for backward compatibility.

    Existing code using ``config.section.get("key")``, ``config.section["key"]``,
    ``dict(config.section)``, and ``**config.section`` keeps working.
    """

    model_config = {"frozen": True}

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return getattr(self, key)
        except AttributeError:
            return default

    def __getitem__(self, key: str) -> Any:
        try:
            return getattr(self, key)
        except AttributeError:
            raise KeyError(key)

    def keys(self):
        return list(type(self).model_fields.keys())

    def values(self):
        return [getattr(self, k) for k in type(self).model_fields]

    def items(self):
        return [(k, getattr(self, k)) for k in type(self).model_fields]

    def __iter__(self):
        return iter(type(self).model_fields)

    def __contains__(self, key: str) -> bool:
        return key in type(self).model_fields


# ---------------------------------------------------------------------------
# LLM params (shared by llm and vlm)
# ---------------------------------------------------------------------------
class LLMParamsConfig(ConfigMixin):
    temperature: float = 0.1
    timeout: int = 60
    max_retries: int = 2
    logprobs: bool = True


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------
class LLMConfig(LLMParamsConfig):
    base_url: str = ""
    model: str = ""
    api_key: str = Field(default="", repr=False)


# ---------------------------------------------------------------------------
# VLM
# ---------------------------------------------------------------------------
class VLMConfig(LLMParamsConfig):
    base_url: str = ""
    model: str = ""
    api_key: str = Field(default="", repr=False)


# ---------------------------------------------------------------------------
# Semaphore
# ---------------------------------------------------------------------------
class SemaphoreConfig(ConfigMixin):
    llm_semaphore: int = 10
    vlm_semaphore: int = 10


# ---------------------------------------------------------------------------
# Embedder
# ---------------------------------------------------------------------------
class EmbedderConfig(ConfigMixin):
    provider: str = "openai"
    model_name: str = "jinaai/jina-embeddings-v3"
    base_url: str = "http://vllm:8000/v1"
    api_key: str = Field(default="EMPTY", repr=False)
    max_model_len: int = 8192


# ---------------------------------------------------------------------------
# VectorDB
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


RerankerConfig = Annotated[
    InfinityRerankerConfig | OpenAIRerankerConfig,
    Field(discriminator="provider"),
]


def _default_reranker_config() -> InfinityRerankerConfig:
    return InfinityRerankerConfig()


# ---------------------------------------------------------------------------
# MapReduce
# ---------------------------------------------------------------------------
class MapReduceConfig(ConfigMixin):
    initial_batch_size: int = 10
    expansion_batch_size: int = 5
    max_total_documents: int = 20
    debug: bool = False


# ---------------------------------------------------------------------------
# Verbose
# ---------------------------------------------------------------------------
class VerboseConfig(ConfigMixin):
    level: str = "DEBUG"


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------
class ServerConfig(ConfigMixin):
    preferred_url_scheme: str | None = None


# ---------------------------------------------------------------------------
# LLM Context
# ---------------------------------------------------------------------------
class LLMContextConfig(ConfigMixin):
    max_llm_context_size: int = 8192
    max_output_tokens: int = 1024


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
    api_key: str = Field(default="EMPTY", repr=False)
    model_name: str = "openai/whisper-large-v3-turbo"
    timeout: int = 3600
    max_concurrent_chunks: int = 20
    use_whisper_lang_detector: bool = True


# ---------------------------------------------------------------------------
# OpenAI Loader (nested under loader)
# ---------------------------------------------------------------------------
class OpenAILoaderConfig(ConfigMixin):
    base_url: str = "http://openai:8000/v1"
    api_key: str = Field(default="EMPTY", repr=False)
    model: str = "dotsocr-model"
    temperature: float = 0.2
    timeout: int = 180
    max_retries: int = 2
    top_p: float = 0.9
    concurrency_limit: int = 20


# ---------------------------------------------------------------------------
# Local Whisper (nested under loader)
# ---------------------------------------------------------------------------
class LocalWhisperConfig(ConfigMixin):
    model: str = "base"
    whisper_n_workers: int = 3
    whisper_num_gpus: float = 0.01
    whisper_concurrency_per_worker: int = 2
    whisper_timeout: int = 1800
    whisper_max_task_retry: int = 1
    whisper_retry_base_delay: float = 2.0


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
    wav: str = "LocalWhisperLoader"
    mp3: str = "LocalWhisperLoader"
    flac: str = "LocalWhisperLoader"
    ogg: str = "LocalWhisperLoader"
    aac: str = "LocalWhisperLoader"
    flv: str = "LocalWhisperLoader"
    wma: str = "LocalWhisperLoader"
    mp4: str = "LocalWhisperLoader"
    md: str = "MarkdownLoader"


# ---------------------------------------------------------------------------
# Mimetypes mapping (nested under loader)
# ---------------------------------------------------------------------------
class MimetypesConfig(ConfigMixin):
    """Maps MIME type strings to file extensions.

    Stored as regular fields so Pydantic serialization works normally.
    Access via .to_dict() for {mime_type: extension} mapping.
    """

    text_plain: str = Field(default=".txt", alias="text/plain")
    text_markdown: str = Field(default=".md", alias="text/markdown")
    application_pdf: str = Field(default=".pdf", alias="application/pdf")
    message_rfc822: str = Field(default=".eml", alias="message/rfc822")
    application_docx: str = Field(
        default=".docx",
        alias="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    application_pptx: str = Field(
        default=".pptx",
        alias="application/vnd.openxmlformats-officedocument.presentationml.presentation",
    )
    application_msword: str = Field(default=".doc", alias="application/msword")
    image_png: str = Field(default=".png", alias="image/png")
    image_jpeg: str = Field(default=".jpeg", alias="image/jpeg")
    audio_wav: str = Field(default=".wav", alias="audio/wav")
    audio_mpeg: str = Field(default=".mp3", alias="audio/mpeg")
    audio_flac: str = Field(default=".flac", alias="audio/flac")
    audio_ogg: str = Field(default=".ogg", alias="audio/ogg")
    audio_aac: str = Field(default=".aac", alias="audio/aac")
    video_x_flv: str = Field(default=".flv", alias="video/x-flv")
    audio_x_ms_wma: str = Field(default=".wma", alias="audio/x-ms-wma")
    video_mp4: str = Field(default=".mp4", alias="video/mp4")

    model_config = {"frozen": True, "extra": "allow", "populate_by_name": True}

    def to_dict(self) -> dict[str, str]:
        """Return {mime_type: extension} mapping using aliases as keys."""
        result = {}
        for field_name, field_info in type(self).model_fields.items():
            alias = field_info.alias or field_name
            result[alias] = getattr(self, field_name)
        if self.__pydantic_extra__:
            result.update(self.__pydantic_extra__)
        return result


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------
class LoaderConfig(ConfigMixin):
    image_captioning: bool = True
    image_captioning_url: bool = True
    save_markdown: bool = False
    mimetypes: MimetypesConfig = Field(default_factory=MimetypesConfig)
    local_whisper: LocalWhisperConfig = Field(default_factory=LocalWhisperConfig)
    file_loaders: FileLoadersConfig = Field(default_factory=FileLoadersConfig)
    marker_max_tasks_per_child: int = 20
    marker_pool_size: int = 1
    marker_max_processes: int = 2
    marker_num_gpus: float = 0.01
    marker_timeout: int = 3600
    marker_pdftext_workers: int = 2
    marker_chunk_size: int = 10
    marker_max_task_retry: int = 3
    marker_retry_base_delay: float = 2.0
    docling_num_gpus: float = Field(default=0.01, ge=0)
    docling_pool_size: int = Field(default=1, ge=1)
    docling_max_tasks_per_worker: int = Field(default=2, ge=1)
    docling_timeout: int = 3600
    docling_max_task_retry: int = 3
    docling_retry_base_delay: float = 2.0
    transcriber: TranscriberConfig = Field(default_factory=TranscriberConfig)
    openai: OpenAILoaderConfig = Field(default_factory=OpenAILoaderConfig)


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
# Chunker
# ---------------------------------------------------------------------------
class ChunkerConfig(ConfigMixin):
    name: str = "recursive_splitter"
    contextual_retrieval: bool = True
    contextualization_timeout: int = 120
    max_concurrent_contextualization: int = 10
    chunk_size: int = 512
    chunk_overlap_rate: float = 0.2


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
    allow_filterless_fallback: bool = True


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
    fetch_verify_ssl: bool = False


class StaanWebSearchConfig(_BaseWebSearchConfig):
    provider: Literal["staan"] = "staan"
    base_url: str = "https://api.staan.ai/search/web"


WebSearchConfig = Annotated[
    StaanWebSearchConfig,
    Field(discriminator="provider"),
]


# ---------------------------------------------------------------------------
# Root Settings — composes all sub-models
# ---------------------------------------------------------------------------
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
