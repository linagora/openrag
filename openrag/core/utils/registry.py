import threading
from typing import Any, Generic, Type, TypeVar

T = TypeVar("T")


class RegistryError(Exception):
    """Raised when a registry lookup or registration fails."""


class Registry(Generic[T]):
    """Generic, thread-safe registry for mapping string keys to implementation classes.

    Usage::

        embedder_registry: Registry[Embedder] = Registry("embedder")

        @embedder_registry.register("vllm")
        class VLLMEmbedder(Embedder): ...

        instance = embedder_registry.create("vllm", endpoint="http://...", model_name="bge-m3")
    """

    def __init__(self, kind: str = "") -> None:
        self._kind = kind
        self._registry: dict[str, Type[T]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, name: str):
        """Decorator to register a class under a name.

        Example::

            @embedder_registry.register("vllm")
            class VLLMEmbedder(Embedder): ...
        """

        def decorator(cls: Type[T]) -> Type[T]:
            with self._lock:
                if name in self._registry:
                    raise RegistryError(
                        f"{self._kind} registry: key '{name}' already registered "
                        f"to {self._registry[name].__name__}, "
                        f"cannot register {cls.__name__}"
                    )
                self._registry[name] = cls
            return cls

        return decorator

    # ------------------------------------------------------------------
    # Lookup & instantiation
    # ------------------------------------------------------------------

    def get(self, name: str) -> Type[T]:
        """Get a registered class by name. Raises RegistryError if not found."""
        cls = self._registry.get(name)
        if cls is None:
            available = ", ".join(sorted(self._registry.keys()))
            raise RegistryError(
                f"{self._kind} '{name}' not found. Available: [{available}]"
            )
        return cls

    def create(self, name: str, *args: Any, **kwargs: Any) -> T:
        """Instantiate a registered class by name."""
        cls = self.get(name)
        return cls(*args, **kwargs)

    # ------------------------------------------------------------------
    # Inspection
    # ------------------------------------------------------------------

    def keys(self) -> list[str]:
        """Return all registered keys."""
        return list(self._registry.keys())

    def __contains__(self, name: str) -> bool:
        return name in self._registry

    def __len__(self) -> int:
        return len(self._registry)

    def __repr__(self) -> str:
        return f"Registry(kind='{self._kind}', keys={self.keys()})"
