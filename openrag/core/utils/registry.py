"""Generic registry pattern for pluggable components.

Usage:
    from openrag.core.utils.registry import Registry

    embedder_registry: Registry[Embedder] = Registry("embedder")

    @embedder_registry.register("vllm")
    class VLLMEmbedder(Embedder):
        ...

    instance = embedder_registry.create("vllm", endpoint="http://...")
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Generic, Type, TypeVar

T = TypeVar("T")


class RegistryError(Exception):
    """Raised when a registry lookup fails."""

    pass


class Registry(Generic[T]):
    """Generic type-safe registry mapping string names to component classes.

    Each component domain (embedder, reranker, llm, vlm, chunking, parser)
    has its own Registry instance. Implementations register via the
    ``@registry.register("name")`` decorator and are instantiated via
    ``registry.create("name", **kwargs)``.
    """

    def __init__(self, kind: str) -> None:
        self._kind = kind
        self._registry: dict[str, Type[T]] = {}

    def register(self, name: str) -> Callable[[Type[T]], Type[T]]:
        """Decorator to register a class under *name*."""

        def decorator(cls: Type[T]) -> Type[T]:
            self._registry[name] = cls
            return cls

        return decorator

    def create(self, name: str, **kwargs: Any) -> T:
        """Instantiate a registered class by name."""
        cls = self._registry.get(name)
        if cls is None:
            available = ", ".join(sorted(self._registry))
            raise RegistryError(
                f"{self._kind} '{name}' not found. Available: [{available}]"
            )
        return cls(**kwargs)

    def get_class(self, name: str) -> Type[T]:
        """Return the registered class without instantiating."""
        cls = self._registry.get(name)
        if cls is None:
            available = ", ".join(sorted(self._registry))
            raise RegistryError(
                f"{self._kind} '{name}' not found. Available: [{available}]"
            )
        return cls

    def list_registered(self) -> list[str]:
        """Return sorted list of registered names."""
        return sorted(self._registry)

    def __contains__(self, name: str) -> bool:
        return name in self._registry
