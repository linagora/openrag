"""Embedder ABC + registry."""

from .embedder import Embedder
from .registry import embedder_registry

__all__ = ["Embedder", "embedder_registry"]
