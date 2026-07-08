"""Chunking strategy registry."""

from openrag.core.utils.registry import Registry

from .chunking_strategy import ChunkingStrategy

chunking_registry: Registry[ChunkingStrategy] = Registry("chunking")
