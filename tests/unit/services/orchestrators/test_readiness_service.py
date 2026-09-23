import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from core.config.model_endpoints import ModelEndpointConfig
from core.models.readiness import ConfigurationReferenceFinding, ModelEndpointDiscovery, ModelEndpointTarget
from fastapi.encoders import jsonable_encoder
from services.orchestrators import readiness_service as readiness_module
from services.orchestrators.readiness_service import ReadinessService, check_model_endpoint


async def test_checks_run_concurrently_and_share_cached_result():
    entered = 0
    all_entered = asyncio.Event()

    async def check():
        nonlocal entered
        entered += 1
        if entered == 2:
            all_entered.set()
        await all_entered.wait()

    service = ReadinessService({"postgres": check, "milvus": check})
    results = await asyncio.gather(*(service.check() for _ in range(3)))
    assert results == [{"postgres": "ok", "milvus": "ok"}] * 3
    assert entered == 2


async def test_snapshot_checks_cannot_be_mutated_through_cached_or_published_result():
    published = []
    service = ReadinessService({"postgres": AsyncMock()}, publish=published.append)

    snapshot = await service.snapshot()

    assert published == [snapshot]
    with pytest.raises(TypeError):
        snapshot.checks["postgres"] = "unavailable"

    compatibility = await service.check()
    compatibility["postgres"] = "unavailable"

    cached = await service.snapshot()
    assert cached is snapshot
    assert cached.checks == {"postgres": "ok"}
    assert jsonable_encoder(cached.checks) == {"postgres": "ok"}


async def test_cached_snapshot_is_json_serializable():
    service = ReadinessService({"postgres": AsyncMock()})

    cached = await service.snapshot()

    assert jsonable_encoder(cached) == {
        "checks": {"postgres": "ok"},
        "model_endpoints": [],
        "configuration_references": [],
    }


async def test_outage_recovers_after_cache_expiry():
    check = AsyncMock(side_effect=[RuntimeError("secret connection string"), None])
    service = ReadinessService({"postgres": check}, cache_ttl=0)
    assert await service.check() == {"postgres": "unavailable"}
    assert await service.check() == {"postgres": "ok"}


async def test_hung_check_times_out_without_losing_healthy_results():
    async def hung():
        await asyncio.Event().wait()

    service = ReadinessService({"ray": hung, "postgres": AsyncMock()}, timeout=0.01)
    assert await service.check() == {"ray": "timeout", "postgres": "ok"}


async def test_cancellation_is_not_cached_as_an_outage():
    service = ReadinessService({"ray": AsyncMock(side_effect=asyncio.CancelledError)})
    with pytest.raises(asyncio.CancelledError):
        await service.check()


async def test_snapshot_deduplicates_effective_probe_and_validates_each_model(respx_mock):
    route = respx_mock.get("https://models.test/v1/models").respond(200, json={"data": [{"id": "served"}]})
    discovery = AsyncMock(
        return_value=ModelEndpointDiscovery(
            targets=(
                ModelEndpointTarget(
                    provider="served-provider",
                    kind="llm",
                    config=ModelEndpointConfig(
                        name="served-provider", endpoint="https://models.test/v1", model_name="served"
                    ),
                    is_default=True,
                ),
                ModelEndpointTarget(
                    provider="missing-model",
                    kind="llm",
                    config=ModelEndpointConfig(
                        name="missing-model", endpoint="https://models.test/v1/", model_name="absent"
                    ),
                ),
            )
        )
    )
    service = ReadinessService(
        {"postgres": AsyncMock()},
        discover_model_endpoints=discovery,
        summary_model_kinds=("llm",),
    )

    snapshot = await service.snapshot()

    assert route.call_count == 1
    assert [(item.provider, item.status) for item in snapshot.model_endpoints] == [
        ("missing-model", "unavailable"),
        ("served-provider", "ok"),
    ]
    assert snapshot.checks == {"postgres": "ok", "llm": "ok", "model_endpoint_discovery": "ok"}


async def test_snapshot_deduplicates_canonical_equivalent_probe_urls(respx_mock):
    route = respx_mock.route(method="GET").respond(200, json={"data": [{"id": "served"}]})
    discovery = AsyncMock(
        return_value=ModelEndpointDiscovery(
            targets=tuple(
                ModelEndpointTarget(
                    provider=provider,
                    kind="llm",
                    config=ModelEndpointConfig(endpoint=endpoint, model_name="served"),
                )
                for provider, endpoint in (
                    ("canonical", "https://models.test/v1"),
                    ("mixed-case", "https://MODELS.TEST/v1"),
                    ("default-port", "https://models.test:443/v1"),
                )
            )
        )
    )

    snapshot = await ReadinessService({}, discover_model_endpoints=discovery).snapshot()

    assert route.call_count == 1
    assert route.calls[0].request.url == httpx.URL("https://models.test/v1/models")
    assert [(item.provider, item.status) for item in snapshot.model_endpoints] == [
        ("canonical", "ok"),
        ("default-port", "ok"),
        ("mixed-case", "ok"),
    ]


async def test_snapshot_separates_identical_urls_with_different_api_keys(respx_mock):
    route = respx_mock.get("https://models.test/v1/models").respond(200, json={"data": [{"id": "served"}]})
    discovery = AsyncMock(
        return_value=ModelEndpointDiscovery(
            targets=tuple(
                ModelEndpointTarget(
                    provider=provider,
                    kind="llm",
                    config=ModelEndpointConfig(
                        name=provider,
                        endpoint="https://models.test/v1",
                        model_name="served",
                        extra={"api_key": api_key},
                    ),
                )
                for provider, api_key in (("first", "first-key"), ("second", "second-key"))
            )
        )
    )

    snapshot = await ReadinessService({}, discover_model_endpoints=discovery).snapshot()

    assert route.call_count == 2
    assert {call.request.headers["Authorization"] for call in route.calls} == {
        "Bearer first-key",
        "Bearer second-key",
    }
    assert [(item.provider, item.status) for item in snapshot.model_endpoints] == [
        ("first", "ok"),
        ("second", "ok"),
    ]


@pytest.mark.parametrize(
    "kind,implementation,base,path",
    [
        ("llm", "vllm", "/v1/", "/v1/models"),
        ("llm", "ollama", "", "/v1/models"),
        ("llm", "ollama", "/v1", "/v1/models"),
        ("reranker", "infinity", "", "/health"),
        ("reranker", "tei", "", "/health"),
    ],
)
async def test_snapshot_uses_provider_specific_probe_path(respx_mock, kind, implementation, base, path):
    route = respx_mock.get("https://model.test" + path).respond(200, json={"data": [{"id": "model"}]})
    discovery = AsyncMock(
        return_value=ModelEndpointDiscovery(
            targets=(
                ModelEndpointTarget(
                    provider="provider",
                    kind=kind,
                    config=ModelEndpointConfig(
                        name="provider",
                        endpoint="https://model.test" + base,
                        model_name="model",
                        extra={"implementation": implementation},
                    ),
                ),
            )
        )
    )

    snapshot = await ReadinessService({}, discover_model_endpoints=discovery).snapshot()

    assert route.call_count == 1
    assert snapshot.model_endpoints[0].status == "ok"


async def test_timed_out_probe_group_does_not_hide_healthy_group(respx_mock):
    respx_mock.get("https://slow.test/v1/models").mock(side_effect=httpx.ReadTimeout("timed out"))
    healthy = respx_mock.get("https://healthy.test/v1/models").respond(200, json={"data": [{"id": "served"}]})
    discovery = AsyncMock(
        return_value=ModelEndpointDiscovery(
            targets=(
                ModelEndpointTarget(
                    provider="slow",
                    kind="llm",
                    config=ModelEndpointConfig(endpoint="https://slow.test/v1", model_name="served"),
                ),
                ModelEndpointTarget(
                    provider="healthy",
                    kind="llm",
                    config=ModelEndpointConfig(endpoint="https://healthy.test/v1", model_name="served"),
                ),
            )
        )
    )

    snapshot = await ReadinessService({}, discover_model_endpoints=discovery).snapshot()

    assert healthy.call_count == 1
    assert [(item.provider, item.status) for item in snapshot.model_endpoints] == [
        ("healthy", "ok"),
        ("slow", "timeout"),
    ]


async def test_discovery_and_model_probes_share_one_timeout_budget(respx_mock):
    async def never_respond(_request):
        await asyncio.Event().wait()

    respx_mock.get("https://slow.test/v1/models").mock(side_effect=never_respond)

    async def discover():
        await asyncio.sleep(0.15)
        return ModelEndpointDiscovery(
            targets=(
                ModelEndpointTarget(
                    provider="slow",
                    kind="llm",
                    config=ModelEndpointConfig(endpoint="https://slow.test/v1", model_name="served"),
                ),
            )
        )

    service = ReadinessService({}, discover_model_endpoints=discover, timeout=0.2)
    started = asyncio.get_running_loop().time()

    snapshot = await service.snapshot()

    elapsed = asyncio.get_running_loop().time() - started
    assert elapsed < 0.3
    assert [(item.provider, item.status) for item in snapshot.model_endpoints] == [("slow", "timeout")]
    assert snapshot.checks == {"model_endpoint_discovery": "ok"}


async def test_malformed_model_list_entry_marks_endpoint_unavailable(respx_mock):
    respx_mock.get("https://models.test/v1/models").respond(
        200,
        json={"data": [{"id": "served"}, {}]},
    )
    discovery = AsyncMock(
        return_value=ModelEndpointDiscovery(
            targets=(
                ModelEndpointTarget(
                    provider="malformed",
                    kind="llm",
                    config=ModelEndpointConfig(endpoint="https://models.test/v1", model_name="served"),
                ),
            )
        )
    )

    snapshot = await ReadinessService({}, discover_model_endpoints=discovery).snapshot()

    assert [(item.provider, item.status) for item in snapshot.model_endpoints] == [("malformed", "unavailable")]


async def test_missing_endpoint_config_is_unresolvable_without_http_request(respx_mock):
    discovery = AsyncMock(
        return_value=ModelEndpointDiscovery(targets=(ModelEndpointTarget(provider="deleted", kind="stt", config=None),))
    )

    snapshot = await ReadinessService({}, discover_model_endpoints=discovery).snapshot()

    assert len(respx_mock.calls) == 0
    assert [(item.provider, item.kind, item.status) for item in snapshot.model_endpoints] == [
        ("deleted", "stt", "unresolvable")
    ]
    assert snapshot.checks == {"model_endpoint_discovery": "ok"}


async def test_missing_required_default_is_unresolvable_and_drives_summary():
    discovery = AsyncMock(return_value=ModelEndpointDiscovery())
    service = ReadinessService({}, discover_model_endpoints=discovery, summary_model_kinds=("llm",))

    snapshot = await service.snapshot()

    assert [(item.provider, item.kind, item.status) for item in snapshot.model_endpoints] == [
        ("default", "llm", "unresolvable")
    ]
    assert snapshot.checks == {"llm": "unresolvable", "model_endpoint_discovery": "ok"}


async def test_missing_configuration_references_are_aggregated_without_names():
    discovery = AsyncMock(
        return_value=ModelEndpointDiscovery(
            configuration_references=(
                ConfigurationReferenceFinding(kind="retrieval_preset", name="beta"),
                ConfigurationReferenceFinding(kind="indexation_preset", name="zulu"),
                ConfigurationReferenceFinding(kind="indexation_preset", name="alpha"),
            )
        )
    )

    snapshot = await ReadinessService({}, discover_model_endpoints=discovery).snapshot()

    assert [(item.kind, item.count, item.status) for item in snapshot.configuration_references] == [
        ("indexation_preset", 2, "unresolvable"),
        ("retrieval_preset", 1, "unresolvable"),
    ]


@pytest.mark.parametrize(
    "failure,expected_status",
    [(TimeoutError("secret timeout"), "timeout"), (RuntimeError("secret connection string"), "unavailable")],
)
async def test_discovery_failure_returns_no_details_or_exception_text(failure, expected_status):
    discovery = AsyncMock(side_effect=failure)

    snapshot = await ReadinessService({"postgres": AsyncMock()}, discover_model_endpoints=discovery).snapshot()

    assert snapshot.model_endpoints == ()
    assert snapshot.configuration_references == ()
    assert snapshot.checks == {"postgres": "ok", "model_endpoint_discovery": expected_status}
    assert "secret" not in repr(snapshot)


async def test_discovery_cancellation_propagates():
    discovery = AsyncMock(side_effect=asyncio.CancelledError)

    with pytest.raises(asyncio.CancelledError):
        await ReadinessService({}, discover_model_endpoints=discovery).snapshot()


async def test_probe_cancellation_propagates(respx_mock):
    async def cancel_request(_request):
        raise asyncio.CancelledError

    respx_mock.get("https://models.test/v1/models").mock(side_effect=cancel_request)
    discovery = AsyncMock(
        return_value=ModelEndpointDiscovery(
            targets=(
                ModelEndpointTarget(
                    provider="cancelled",
                    kind="llm",
                    config=ModelEndpointConfig(endpoint="https://models.test/v1", model_name="served"),
                ),
            )
        )
    )

    with pytest.raises(asyncio.CancelledError):
        await ReadinessService({}, discover_model_endpoints=discovery).snapshot()


async def test_snapshot_runs_core_checks_and_discovery_concurrently():
    core_entered = asyncio.Event()
    discovery_entered = asyncio.Event()

    async def check_core():
        core_entered.set()
        await discovery_entered.wait()

    async def discover():
        discovery_entered.set()
        await core_entered.wait()
        return ModelEndpointDiscovery()

    snapshot = await ReadinessService({"postgres": check_core}, discover_model_endpoints=discover).snapshot()

    assert snapshot.checks == {"postgres": "ok", "model_endpoint_discovery": "ok"}


async def test_snapshot_check_and_repeat_calls_share_refresh_until_two_second_ttl(monkeypatch):
    class Clock:
        now = 100.0

        @classmethod
        def monotonic(cls):
            return cls.now

    monkeypatch.setattr(readiness_module, "time", Clock)
    core_calls = 0
    discovery_calls = 0
    published = []

    async def check_core():
        nonlocal core_calls
        core_calls += 1

    async def discover():
        nonlocal discovery_calls
        discovery_calls += 1
        return ModelEndpointDiscovery()

    service = ReadinessService(
        {"postgres": check_core},
        discover_model_endpoints=discover,
        publish=published.append,
    )

    first, compatibility, concurrent = await asyncio.gather(service.snapshot(), service.check(), service.snapshot())

    assert first is concurrent
    assert compatibility == {"postgres": "ok", "model_endpoint_discovery": "ok"}
    assert core_calls == discovery_calls == 1
    assert published == [first]
    assert await service.snapshot() is first

    Clock.now = 101.99
    assert await service.snapshot() is first
    assert core_calls == discovery_calls == 1

    Clock.now = 102.0
    refreshed = await service.snapshot()
    assert refreshed is not first
    assert core_calls == discovery_calls == 2
    assert published == [first, refreshed]


async def test_publication_failure_does_not_break_or_repeat_a_valid_refresh():
    check = AsyncMock()
    publish = MagicMock(side_effect=RuntimeError("metrics backend failed"))
    service = ReadinessService({"postgres": check}, publish=publish)

    first = await service.snapshot()
    cached = await service.snapshot()

    assert first is cached
    assert first.checks == {"postgres": "ok"}
    check.assert_awaited_once()
    publish.assert_called_once_with(first)


async def test_publication_cancellation_propagates_after_caching_the_refresh():
    check = AsyncMock()

    def publish(_snapshot):
        raise asyncio.CancelledError

    service = ReadinessService({"postgres": check}, publish=publish)

    with pytest.raises(asyncio.CancelledError):
        await service.snapshot()

    cached = await service.snapshot()
    assert cached.checks == {"postgres": "ok"}
    check.assert_awaited_once()


@pytest.mark.parametrize(
    "implementation,base,path",
    [
        ("vllm", "/v1/", "/v1/models"),
        ("ollama", "", "/v1/models"),
        ("ollama", "/v1", "/v1/models"),
        ("infinity", "", "/health"),
        ("tei", "", "/health"),
    ],
)
async def test_model_probe_uses_provider_path_and_credentials(respx_mock, implementation, base, path):
    probe = respx_mock.get("https://model.test" + path).respond(200, json={"data": [{"id": "model"}]})
    config = ModelEndpointConfig(
        endpoint="https://model.test" + base,
        model_name="model",
        extra={"implementation": implementation, "api_key": "test-key"},
    )
    await check_model_endpoint(config)
    assert probe.calls[0].request.headers["Authorization"] == "Bearer test-key"


async def test_model_probe_sends_api_key_over_http(respx_mock):
    probe = respx_mock.get("http://model.test/v1/models").respond(200, json={"data": [{"id": "model"}]})
    config = ModelEndpointConfig(
        endpoint="http://model.test/v1",
        model_name="model",
        extra={"api_key": "test-key"},
    )

    await check_model_endpoint(config)

    assert probe.calls[0].request.headers["Authorization"] == "Bearer test-key"


@pytest.mark.parametrize("api_key", ["EMPTY", "  EMPTY  ", "   "])
async def test_model_probe_treats_placeholder_api_keys_as_credential_free(respx_mock, api_key):
    probe = respx_mock.get("http://model.test/v1/models").respond(200, json={"data": [{"id": "model"}]})
    config = ModelEndpointConfig(
        endpoint="http://model.test/v1",
        model_name="model",
        extra={"api_key": api_key},
    )

    await check_model_endpoint(config)

    assert probe.called
    assert "Authorization" not in probe.calls[0].request.headers


@pytest.mark.parametrize("scheme", ["http", "https"])
async def test_model_probe_preserves_embedded_credentials(respx_mock, scheme):
    probe = respx_mock.get(f"{scheme}://model.test/v1/models").respond(200, json={"data": [{"id": "model"}]})
    config = ModelEndpointConfig(
        endpoint=f"{scheme}://user:password@model.test/v1",
        model_name="model",
    )

    await check_model_endpoint(config)

    assert probe.called
    assert probe.calls[0].request.headers["Authorization"].startswith("Basic ")


async def test_readiness_probes_credential_bearing_http_endpoint(respx_mock):
    probe = respx_mock.get("http://model.test/v1/models").respond(200, json={"data": [{"id": "model"}]})
    discovery = AsyncMock(
        return_value=ModelEndpointDiscovery(
            targets=(
                ModelEndpointTarget(
                    provider="internal",
                    kind="llm",
                    config=ModelEndpointConfig(
                        endpoint="http://model.test/v1",
                        model_name="model",
                        extra={"api_key": "test-key"},
                    ),
                ),
            )
        )
    )

    snapshot = await ReadinessService({}, discover_model_endpoints=discovery).snapshot()

    assert probe.calls[0].request.headers["Authorization"] == "Bearer test-key"
    assert [(item.provider, item.status) for item in snapshot.model_endpoints] == [("internal", "ok")]


async def test_model_probe_preserves_credential_free_http(respx_mock):
    probe = respx_mock.get("http://model.test/v1/models").respond(200, json={"data": [{"id": "model"}]})
    config = ModelEndpointConfig(endpoint="http://model.test/v1", model_name="model")

    await check_model_endpoint(config)

    assert probe.called
    assert "Authorization" not in probe.calls[0].request.headers


async def test_reranker_defaults_to_infinity_health_probe(respx_mock):
    probe = respx_mock.get("https://model.test/health").respond(200)
    config = ModelEndpointConfig(endpoint="https://model.test", model_name="reranker")
    await check_model_endpoint(config, model_type="reranker")
    assert probe.called


@pytest.mark.parametrize("status,payload", [(401, {}), (503, {}), (200, {"data": []}), (200, {})])
async def test_model_probe_rejects_unavailable_or_missing_model(respx_mock, status, payload):
    respx_mock.get("https://model.test/v1/models").respond(status, json=payload)
    config = ModelEndpointConfig(endpoint="https://model.test/v1", model_name="model")
    service = ReadinessService({"llm": lambda: check_model_endpoint(config)})
    assert await service.check() == {"llm": "unavailable"}


async def test_httpx_timeout_is_reported_as_timeout(monkeypatch):
    async def timed_out():
        raise httpx.ReadTimeout("model timed out")

    service = ReadinessService({"llm": timed_out})
    assert await service.check() == {"llm": "timeout"}
