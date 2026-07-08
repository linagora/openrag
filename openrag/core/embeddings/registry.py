"""Embedder registry."""

from openrag.core.utils.registry import Registry

from .embedder import Embedder

embedder_registry: Registry[Embedder] = Registry("embedder")
