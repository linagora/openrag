import httpx
import pytest
from core.utils.exceptions import InferenceConnectionError, LLMParsingError
from services.inference._circuit_breaker import (
    _breaker_config,
    _breakers,
    get_breaker,
    with_circuit_breaker,
)


@pytest.fixture(autouse=True)
def _clean_breakers():
    for breaker in _breakers.values():
        breaker.close()
    _breakers.clear()
    _breaker_config.clear()
    yield
    for breaker in _breakers.values():
        breaker.close()
    _breakers.clear()
    _breaker_config.clear()


class TestGetBreaker:
    def test_returns_same_instance(self):
        b1 = get_breaker("llm")
        b2 = get_breaker("llm")
        assert b1 is b2

    def test_different_names_different_instances(self):
        b1 = get_breaker("llm")
        b2 = get_breaker("embedder")
        assert b1 is not b2

    def test_default_fail_max_is_50(self):
        b = get_breaker("test-default")
        assert b.fail_max == 50


class TestExclusions:
    @pytest.mark.asyncio
    async def test_client_4xx_excluded(self):
        breaker = get_breaker("test-4xx", fail_max=2, timeout_duration=1.0)

        async def fail_4xx():
            req = httpx.Request("GET", "http://test")
            raise httpx.HTTPStatusError("bad request", request=req, response=httpx.Response(400, request=req))

        for _ in range(5):
            with pytest.raises(httpx.HTTPStatusError):
                await breaker.call_async(fail_4xx)

        assert "Closed" in type(breaker.state).__name__

    @pytest.mark.asyncio
    async def test_llm_parsing_error_excluded(self):
        breaker = get_breaker("test-parse", fail_max=2, timeout_duration=1.0)

        async def fail_parse():
            raise LLMParsingError(raw_response="not json")

        for _ in range(5):
            with pytest.raises(LLMParsingError):
                await breaker.call_async(fail_parse)

        assert "Closed" in type(breaker.state).__name__

    @pytest.mark.asyncio
    async def test_server_5xx_trips_breaker(self):
        breaker = get_breaker("test-5xx", fail_max=2, timeout_duration=1.0)

        async def fail_5xx():
            req = httpx.Request("GET", "http://test")
            raise httpx.HTTPStatusError("bad gateway", request=req, response=httpx.Response(502, request=req))

        with pytest.raises(httpx.HTTPStatusError):
            await breaker.call_async(fail_5xx)

        from aiobreaker import CircuitBreakerError

        with pytest.raises(CircuitBreakerError):
            await breaker.call_async(fail_5xx)

        assert "Open" in type(breaker.state).__name__


class TestWithCircuitBreaker:
    @pytest.mark.asyncio
    async def test_passes_through_on_success(self):
        @with_circuit_breaker("test-ok", fail_max=3, timeout_duration=1.0)
        async def ok():
            return "result"

        assert await ok() == "result"

    @pytest.mark.asyncio
    async def test_raises_inference_connection_error_when_open(self):
        call_count = 0

        @with_circuit_breaker("test-open", fail_max=2, timeout_duration=60.0)
        async def always_fail():
            nonlocal call_count
            call_count += 1
            raise ConnectionError("down")

        with pytest.raises(ConnectionError):
            await always_fail()

        with pytest.raises(InferenceConnectionError, match="Circuit open"):
            await always_fail()

        with pytest.raises(InferenceConnectionError, match="Circuit open"):
            await always_fail()

        assert call_count == 2
