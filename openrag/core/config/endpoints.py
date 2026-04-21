"""Model endpoint configuration — LLM, VLM, embedder, semaphore settings."""

from __future__ import annotations

from pydantic import Field

from openrag.core.config.base import ConfigMixin


class LLMParamsConfig(ConfigMixin):
    """Shared parameters for LLM/VLM endpoints."""

    temperature: float = 0.1
    timeout: int = 60
    max_retries: int = 2
    logprobs: bool = True


class LLMConfig(LLMParamsConfig):
    """LLM endpoint configuration."""

    base_url: str = ""
    model: str = ""
    api_key: str = Field(default="", repr=False)


class VLMConfig(LLMParamsConfig):
    """Vision-Language Model endpoint configuration."""

    base_url: str = ""
    model: str = ""
    api_key: str = Field(default="", repr=False)


class EmbedderConfig(ConfigMixin):
    """Embedding model endpoint configuration."""

    provider: str = "openai"
    model_name: str = "jinaai/jina-embeddings-v3"
    base_url: str = "http://vllm:8000/v1"
    api_key: str = Field(default="EMPTY", repr=False)
    max_model_len: int = 8192


class SemaphoreConfig(ConfigMixin):
    """Concurrency limits for LLM and VLM calls."""

    llm_semaphore: int = 10
    vlm_semaphore: int = 10


class LLMContextConfig(ConfigMixin):
    """Token budget settings for LLM context."""

    max_llm_context_size: int = 8192
    max_output_tokens: int = 1024
