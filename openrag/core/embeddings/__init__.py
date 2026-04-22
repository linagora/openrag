"""Embedder ABC + registry."""

from openrag.core.embeddings.embedder import Embedder
from openrag.core.embeddings.registry import embedder_registry

__all__ = ["Embedder", "embedder_registry"]
