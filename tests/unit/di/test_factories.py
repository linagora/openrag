"""Tests for the generic cached component factory (Phase 11A)."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

import pytest
from core.utils.registry import Registry
from di.factories import make_component_factory


@dataclass
class _FakeEndpoint:
    """Stand-in for the forward-looking ``ModelEndpointConfig`` shape."""

    endpoint: str = "http://host:8000/v1"
    model_name: str = "m"
    batch_size: int = 8
    timeout: float = 30.0
    extra: dict[str, Any] = field(default_factory=dict)


class _Dummy:
    """Records the kwargs it was built with so tests can assert on them."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


def _registry() -> Registry[_Dummy]:
    reg: Registry[_Dummy] = Registry("dummy")
    reg.register("vllm")(_Dummy)
    reg.register("other")(type("_Other", (_Dummy,), {}))
    return reg


class TestMakeComponentFactory:
    def test_builds_and_caches_single_instance(self):
        """Repeated calls for the same name reuse the first-built instance."""
        caches: list[dict[str, _Dummy]] = []
        factory, cache = make_component_factory(
            _registry(),
            {"default": _FakeEndpoint()},
            default_impl="vllm",
            client_caches=caches,
        )

        first = factory("default")
        second = factory("default")

        assert first is second
        assert cache["default"] is first

    def test_cache_registered_in_client_caches(self):
        """The returned cache is appended to ``client_caches`` for shutdown."""
        caches: list[dict[str, _Dummy]] = []
        _, cache = make_component_factory(
            _registry(),
            {"default": _FakeEndpoint()},
            default_impl="vllm",
            client_caches=caches,
        )

        assert caches == [cache]

    def test_unknown_name_raises_key_error(self):
        """A name absent from the config section raises ``KeyError``."""
        factory, _ = make_component_factory(
            _registry(),
            {"default": _FakeEndpoint()},
            default_impl="vllm",
            client_caches=[],
        )

        with pytest.raises(KeyError, match="missing"):
            factory("missing")

    def test_default_impl_used_when_unspecified(self):
        """Entries without an ``implementation`` key build ``default_impl``."""
        factory, _ = make_component_factory(
            _registry(),
            {"default": _FakeEndpoint()},
            default_impl="vllm",
            client_caches=[],
        )

        instance = factory("default")

        assert type(instance).__name__ == "_Dummy"

    def test_implementation_key_selects_impl_and_is_not_a_kwarg(self):
        """``extra['implementation']`` selects the class but is not forwarded."""
        factory, _ = make_component_factory(
            _registry(),
            {"default": _FakeEndpoint(extra={"implementation": "other"})},
            default_impl="vllm",
            client_caches=[],
        )

        instance = factory("default")

        assert type(instance).__name__ == "_Other"
        assert "implementation" not in instance.kwargs

    def test_llm_token_budgets_are_not_forwarded_as_kwargs(self):
        """The admin-configured LLM token budgets must NOT reach the constructor.

        ``max_llm_context_size`` / ``max_output_tokens`` live in the endpoint's
        ``extra`` but are OpenRAG-side sizing settings read by the chat token
        preflight — they are not OpenAI request fields. Forwarding them lands
        them in the client's ``self._defaults``, which is splatted into *every*
        outbound request body, so a strict provider would start 400-ing as soon
        as an admin saved a budget. Same leak class as batch_size (#712).
        """
        factory, _ = make_component_factory(
            _registry(),
            {
                "default": _FakeEndpoint(
                    extra={
                        "api_key": "secret",
                        "max_llm_context_size": 8192,
                        "max_output_tokens": 1024,
                    }
                )
            },
            default_impl="vllm",
            client_caches=[],
        )

        kwargs = factory("default").kwargs

        assert "max_llm_context_size" not in kwargs
        assert "max_output_tokens" not in kwargs
        # Genuinely impl-specific extras still get through.
        assert kwargs["api_key"] == "secret"

    def test_config_fields_and_extra_forwarded_as_kwargs(self):
        """Endpoint fields plus impl-specific ``extra`` reach the constructor."""
        factory, _ = make_component_factory(
            _registry(),
            {"default": _FakeEndpoint(extra={"api_key": "secret"})},
            default_impl="vllm",
            client_caches=[],
        )

        kwargs = factory("default").kwargs

        assert kwargs["endpoint"] == "http://host:8000/v1"
        assert kwargs["model_name"] == "m"
        assert kwargs["timeout"] == 30.0
        assert kwargs["api_key"] == "secret"

    def test_batch_size_is_not_forwarded_generically(self):
        """batch_size must NOT reach LLM/VLM/reranker constructors (#712).

        Only the embedder consumes it; the VLLM chat/vision clients absorb
        unknown kwargs into ``self._defaults`` and splat them into the request
        body, so an unconditional batch_size leaked a bogus field onto every
        chat and caption call. The embedder factory injects it explicitly via
        ``extra_kwargs_fn`` instead — see ``test_extra_kwargs_fn_merges_last``
        for the inject-and-win path.
        """
        factory, _ = make_component_factory(
            _registry(),
            {"default": _FakeEndpoint(batch_size=64)},
            default_impl="vllm",
            client_caches=[],
        )

        assert "batch_size" not in factory("default").kwargs

    def test_extra_kwargs_fn_can_reinject_batch_size_for_the_embedder(self):
        """The embedder factory supplies batch_size through extra_kwargs_fn."""
        factory, _ = make_component_factory(
            _registry(),
            {"default": _FakeEndpoint(batch_size=64)},
            default_impl="vllm",
            client_caches=[],
            extra_kwargs_fn=lambda cfg: {"batch_size": cfg.batch_size},
        )

        assert factory("default").kwargs["batch_size"] == 64

    def test_extra_kwargs_fn_merges_last(self):
        """``extra_kwargs_fn`` output overrides config-derived kwargs."""
        factory, _ = make_component_factory(
            _registry(),
            {"default": _FakeEndpoint(model_name="from-config")},
            default_impl="vllm",
            client_caches=[],
            extra_kwargs_fn=lambda cfg: {"model_name": "overridden"},
        )

        assert factory("default").kwargs["model_name"] == "overridden"

    def test_concurrent_first_calls_build_once(self):
        """Double-checked locking yields exactly one instance under contention."""
        build_count = 0
        count_lock = threading.Lock()

        class _Counting:
            def __init__(self, **_kwargs: Any) -> None:
                nonlocal build_count
                with count_lock:
                    build_count += 1

        reg: Registry[_Counting] = Registry("counting")
        reg.register("vllm")(_Counting)

        factory, _ = make_component_factory(
            reg,
            {"default": _FakeEndpoint()},
            default_impl="vllm",
            client_caches=[],
        )

        n = 16
        barrier = threading.Barrier(n)
        results: list[_Counting] = []
        results_lock = threading.Lock()

        def worker() -> None:
            barrier.wait()
            instance = factory("default")
            with results_lock:
                results.append(instance)

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert build_count == 1
        assert len(results) == n
        assert all(r is results[0] for r in results)
