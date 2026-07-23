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

from core.config.model_endpoints import LLM_CONTEXT_SIZE_KEY, LLM_OUTPUT_TOKENS_KEY

if TYPE_CHECKING:
    from core.utils.registry import Registry

T = TypeVar("T")

# Keys that live in an endpoint's ``extra`` but must never reach the client
# constructor. ``implementation`` selects the class to build; the two LLM token
# budgets are OpenRAG-side sizing settings consumed by the chat token preflight.
# Any key left here is absorbed into the client's ``self._defaults`` and splatted
# into every outbound request body (see VLLMClient.__init__), which is how
# batch_size once leaked a bogus field onto every call (#712).
_NON_CONSTRUCTOR_EXTRA_KEYS = frozenset({"implementation", LLM_CONTEXT_SIZE_KEY, LLM_OUTPUT_TOKENS_KEY})


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
            # `implementation` selects which class to build, and the LLM token
            # budgets are OpenRAG-side sizing settings read by the chat token
            # preflight (ModelsConfig.llm_context_size / llm_output_tokens) —
            # none of them are constructor arguments, so they are stripped
            # before splatting `extra`. Leaving the budgets in would land them
            # in the client's ``self._defaults`` and therefore in *every*
            # outbound request body as non-OpenAI fields (a strict provider
            # 400s) — the same leak fixed for batch_size in #712.
            impl_kwargs = {k: v for k, v in model_cfg.extra.items() if k not in _NON_CONSTRUCTOR_EXTRA_KEYS}
            impl = model_cfg.extra.get("implementation", default_impl)
            # NOTE: batch_size is deliberately NOT passed here. Only the embedder
            # constructor consumes it; the LLM/VLM clients absorb unknown kwargs
            # into ``self._defaults`` and splat them into the request body, so an
            # unconditional batch_size leaked a bogus field onto every chat and
            # caption call (#712). The embedder factory injects it via
            # ``extra_kwargs_fn`` instead.
            kwargs: dict[str, Any] = {
                "endpoint": model_cfg.endpoint,
                "model_name": model_cfg.model_name,
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
