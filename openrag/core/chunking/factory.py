"""Factory for configured chunking strategies."""

from typing import Any

import core.chunking.recursive  # noqa: F401
from core.chunking.chunking_strategy import ChunkingStrategy
from core.chunking.registry import chunking_registry
from core.utils.exceptions import RegistryError
from core.utils.text import get_num_tokens

# Fraction of the embedder's context window used as the chunker's hard safety
# bound. Half leaves room for what is prepended before embedding — the
# ``[Source | Page | Section]`` header and, when contextualization is on, an
# LLM-generated ``[CONTEXT]`` block — so a unit that passes the bound still fits
# once those are added. It is a backstop against silent truncation, not a
# packing target: normal chunks sit near ``chunk_size``, far below it.
_EMBEDDER_WINDOW_FRACTION = 2


def resolve_hard_max_tokens(chunker_config: Any, embedder_window: int | None) -> int | None:
    """Hard safety bound for atomic units, in tokens.

    An explicit ``chunker.hard_max_tokens`` always wins. Otherwise it is derived
    from the window of the embedder this partition actually uses, because that
    is what silently truncates: content past it is dropped before it is ever
    embedded. Returns ``None`` when no window is known, which disables the
    net rather than inventing a bound from ``chunk_size`` — ``chunk_size`` says
    nothing about what the embedder can hold.
    """
    explicit = getattr(chunker_config, "hard_max_tokens", None)
    if explicit:
        return int(explicit)
    if embedder_window and embedder_window > 0:
        return max(1, int(embedder_window) // _EMBEDDER_WINDOW_FRACTION)
    return None


def create_chunker(config: Any, embedder_window: int | None = None) -> ChunkingStrategy:
    """Create the configured chunking strategy.

    ``embedder_window`` is the context window of the embedder used by the
    partition being indexed; it is injected rather than read from config so the
    strategies stay pure-domain.
    """
    chunker_params = config.chunker.model_dump()
    name = chunker_params.pop("name")
    chunker_params["hard_max_tokens"] = resolve_hard_max_tokens(config.chunker, embedder_window)

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
