from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from core.config.root import Settings
from core.observability.monitoring import CanaryMetrics
from di import canary as canary_wiring
from prometheus_client import CollectorRegistry


@pytest.fixture
def registry(monkeypatch):
    registry = CollectorRegistry()
    monkeypatch.setattr(canary_wiring, "CANARY_METRICS", CanaryMetrics(registry=registry))
    return registry


def _settings(**canary):
    return Settings(rdb={"password": "test"}, canary=canary)


def test_disabled_canary_starts_nothing_and_says_so(registry):
    assert canary_wiring.start_canary(_settings(enabled=False), container=None) is None
    assert registry.get_sample_value("openrag_canary_enabled") == 0


def test_enabled_canary_without_a_container_still_reports_enabled(registry):
    # Nothing will run, and the staleness alert is what must notice.
    assert canary_wiring.start_canary(_settings(enabled=True, interval_seconds=600), container=None) is None
    assert registry.get_sample_value("openrag_canary_enabled") == 1
    assert registry.get_sample_value("openrag_canary_interval_seconds") == 600


async def test_enabled_canary_is_scheduled_on_the_container_services(registry):
    connect = AsyncMock()
    container = SimpleNamespace(
        indexing_service=object(),
        retrieval_service=object(),
        partition_service=object(),
        user_repo=object(),
        document_repo=object(),
        vector_store=object(),
        job_repo=object(),
        catalog_store=SimpleNamespace(connect=connect),
    )

    scheduler = canary_wiring.start_canary(_settings(enabled=True, initial_delay_seconds=3600), container)
    try:
        assert scheduler is not None
        assert scheduler._service._indexing is container.indexing_service
        assert scheduler._lease._connect is connect
    finally:
        await scheduler.stop()
    # Stopped before the initial delay elapsed: the lease was never contended.
    connect.assert_not_awaited()
