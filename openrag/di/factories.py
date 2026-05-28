"""Generic cached component factory — bridges model config to registries.

Phase 11A. :func:`make_component_factory` is the one pattern the composition
root reuses for all four inference component kinds (embedder, reranker, LLM,
VLM). Given a model name it looks up the matching entry in a config section,
resolves which registered implementation to build, instantiates it through the
:class:`~core.utils.registry.Registry`, and caches the result so subsequent
calls reuse the same client (and its underlying httpx connection pool).

The factory returns a ``(factory_fn, cache)`` tuple. The cache dict is exposed
deliberately:

* **Shutdown.** The container appends every cache to a shared
  ``client_caches`` list and, on teardown, calls ``aclose()`` on each cached
  instance to release httpx connections.
* **Invalidation.** Post-refactoring, when a model endpoint is renamed or
  deleted at runtime, the owning service pops the stale entry
  (``cache.pop(old_name, None)``) so the next call rebuilds against the new
  config.

``config_section`` is typed against the :class:`ModelEndpointConfig` protocol
below — the unified per-endpoint config shape that lands with the DB-backed
model registry. Until that config exists the protocol documents the contract
the factory depends on; any object exposing those attributes (including a test
double) is accepted.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

if TYPE_CHECKING:
    from core.utils.registry import Registry

T = TypeVar("T")


class ModelEndpointConfig(Protocol):
    """Structural shape :func:`make_component_factory` reads off each entry.

    ``extra`` carries implementation-specific keyword arguments plus an
    optional ``implementation`` control key that selects which registered
    class to build (falling back to ``default_impl`` when absent).
    """

    endpoint: str
    model_name: str
    batch_size: int
    timeout: float
    extra: Mapping[str, Any]


def make_component_factory(
    registry: Registry[T],
    config_section: Mapping[str, ModelEndpointConfig],
    default_impl: str,
    client_caches: list[dict[str, T]],
    extra_kwargs_fn: Callable[[ModelEndpointConfig], Mapping[str, Any]] | None = None,
) -> tuple[Callable[[str], T], dict[str, T]]:
    """Build a cached ``(name) -> T`` factory from a registry and config section.

    Returns ``(factory_fn, cache)``. The cache is appended to ``client_caches``
    so the container can close every built client on shutdown. Instances are
    created lazily on first request and reused thereafter; construction is
    guarded by double-checked locking so concurrent first calls for the same
    name build exactly one instance.

    ``extra_kwargs_fn``, when given, computes additional constructor kwargs
    from the config entry (merged last, so it wins on key collisions) — the
    seam for kwargs a kind needs but the unified config does not carry.
    """
    cache: dict[str, T] = {}
    lock = threading.Lock()
    client_caches.append(cache)

    def factory(name: str = "default") -> T:
        if name in cache:
            return cache[name]
        with lock:
            if name in cache:
                return cache[name]
            model_cfg = config_section.get(name)
            if model_cfg is None:
                raise KeyError(f"Unknown model '{name}'. Available: {list(config_section)}")
            # `implementation` is a control key (which class to build), not a
            # constructor argument, so it is read out before splatting `extra`.
            impl_kwargs = {k: v for k, v in model_cfg.extra.items() if k != "implementation"}
            impl = model_cfg.extra.get("implementation", default_impl)
            kwargs: dict[str, Any] = {
                "endpoint": model_cfg.endpoint,
                "model_name": model_cfg.model_name,
                "batch_size": model_cfg.batch_size,
                "timeout": model_cfg.timeout,
                **impl_kwargs,
            }
            if extra_kwargs_fn is not None:
                kwargs.update(extra_kwargs_fn(model_cfg))
            instance = registry.create(impl, **kwargs)
            cache[name] = instance
            return instance

    return factory, cache


__all__ = ["ModelEndpointConfig", "make_component_factory"]
