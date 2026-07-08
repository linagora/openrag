"""Inference service layer — clients, resilience, and concurrency primitives.

Importing this package registers implementations in the core registries
(``llm_registry``, ``embedder_registry``, ``vlm_registry``, ``reranker_registry``)
so they can be created via ``registry.create("name", **kwargs)``.
"""

from ._circuit_breaker import get_breaker, with_circuit_breaker
from ._retry import with_retry
from .distributed_semaphore import DistributedSemaphore, DistributedSemaphoreActor
from .ollama_client import OllamaClient, OllamaEmbedder
from .reranker_clients import InfinityReranker, OpenAIReranker
from .vllm_client import VLLMClient, VLLMEmbedder, VLLMVision

__all__ = [
    "DistributedSemaphore",
    "DistributedSemaphoreActor",
    "InfinityReranker",
    "OllamaClient",
    "OllamaEmbedder",
    "OpenAIReranker",
    "VLLMClient",
    "VLLMEmbedder",
    "VLLMVision",
    "get_breaker",
    "with_circuit_breaker",
    "with_retry",
]
