"""Chunking strategy registry."""

from openrag.core.chunking.chunking_strategy import ChunkingStrategy
from openrag.core.utils.registry import Registry

chunking_registry: Registry[ChunkingStrategy] = Registry("chunking")
