"""Model endpoint configuration — LLM, VLM, embedder, semaphore settings."""

from __future__ import annotations

from pydantic import Field

from .base import ConfigMixin


class LLMParamsConfig(ConfigMixin):
    """Shared parameters for LLM/VLM endpoints."""

    temperature: float = 0.1
    timeout: int = 60
    max_retries: int = 2
    # Default OFF: OpenRAG doesn't consume logprobs, and requesting them
    # unsolicited breaks streaming on some OpenAI-compatible backends (e.g. a
    # LiteLLM proxy crashes serializing the final chunk's ChoiceLogprobs). API
    # clients that want logprobs opt in per request (see OpenAIChatCompletionRequest).
    logprobs: bool = False
    enable_thinking: bool | None = None


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
    # Captioning large images through a shared/slow VLM endpoint routinely
    # takes longer than a chat completion, so this is independently tunable
    # (VLM_TIMEOUT) rather than silently inheriting the int llm.timeout.
    timeout: float = 60.0


class EmbedderConfig(ConfigMixin):
    """Embedding model endpoint configuration."""

    provider: str = "openai"
    model_name: str = "jinaai/jina-embeddings-v3"
    base_url: str = "http://vllm:8000/v1"
    api_key: str = Field(default="EMPTY", repr=False)
    # 2047 (just below the 2048 boundary): the embedder sends
    # truncate_prompt_tokens = max_model_len - 1, avoiding the Qwen3-Embedding
    # context-boundary hang (vllm-project/vllm#29496).
    max_model_len: int = Field(default=2047, gt=0)
    # Constrained > 0: a bad env var should fail at config load, not silently
    # degrade into surprising runtime behavior (VLLMEmbedder rewrites a
    # non-positive batch_size/embed_concurrency to 1 and would pass a <= 0
    # timeout straight into httpx).
    timeout: float = Field(default=120.0, gt=0)
    # Big documents are embedded in slices of `batch_size`, at most
    # `embed_concurrency` requests in flight, to stay within the timeout above.
    batch_size: int = Field(default=32, gt=0)
    embed_concurrency: int = Field(default=4, gt=0)


class SemaphoreConfig(ConfigMixin):
    """Concurrency limits for LLM and VLM calls."""

    llm_semaphore: int = 10
    vlm_semaphore: int = 10


class LLMContextConfig(ConfigMixin):
    """Token budget settings for LLM context."""

    max_llm_context_size: int = 8192
    max_output_tokens: int = 1024
