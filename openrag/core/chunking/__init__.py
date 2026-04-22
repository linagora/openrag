"""ChunkingStrategy ABC + registry."""

from openrag.core.chunking.chunking_strategy import ChunkingStrategy
from openrag.core.chunking.registry import chunking_registry

__all__ = ["ChunkingStrategy", "chunking_registry"]
