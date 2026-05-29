"""Factory for configured chunking strategies."""

from typing import Any

import core.chunking.recursive  # noqa: F401
from core.chunking.chunking_strategy import ChunkingStrategy
from core.chunking.registry import chunking_registry
from core.utils.exceptions import RegistryError
from core.utils.text import get_num_tokens


def create_chunker(config: Any) -> ChunkingStrategy:
    """Create the configured chunking strategy."""
    chunker_params = config.chunker.model_dump()
    name = chunker_params.pop("name")

    try:
        return chunking_registry.create(
            name,
            length_function=get_num_tokens(),
            **chunker_params,
        )
    except RegistryError as exc:
        raise ValueError(
            f"Chunker '{name}' is not recognized. Available chunkers: {chunking_registry.list_registered()}"
        ) from exc
