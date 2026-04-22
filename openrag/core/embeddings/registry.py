"""Embedder registry."""

from openrag.core.embeddings.embedder import Embedder
from openrag.core.utils.registry import Registry

embedder_registry: Registry[Embedder] = Registry("embedder")
