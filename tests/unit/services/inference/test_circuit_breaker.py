import httpx
import pytest
from core.utils.exceptions import CircuitBreakerOpenError, LLMParsingError
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
    async def test_raises_circuit_breaker_open_error_when_open(self):
        call_count = 0

        @with_circuit_breaker("test-open", fail_max=2, timeout_duration=60.0)
        async def always_fail():
            nonlocal call_count
            call_count += 1
            raise ConnectionError("down")

        with pytest.raises(ConnectionError):
            await always_fail()

        with pytest.raises(CircuitBreakerOpenError, match="Circuit breaker open"):
            await always_fail()

        with pytest.raises(CircuitBreakerOpenError, match="Circuit breaker open"):
            await always_fail()

        assert call_count == 2


class TestStateIsExported:
    """``openrag_circuit_breaker_state`` must be written when the breaker moves.

    The export path itself is covered in ``tests/integration/test_ray_metrics_export.py``;
    what is pinned here is that the listener actually calls it. Without this, dropping
    the ``record_circuit_breaker_state`` call leaves the series absent, and
    ``OpenRagCircuitBreakerOpen`` (``max by (name) (openrag_circuit_breaker_state) == 1``,
    severity critical) can never fire — which looks exactly like a breaker that never
    opens.
    """

    @pytest.mark.asyncio
    async def test_opening_the_breaker_records_the_open_state(self, monkeypatch):
        import services.inference._circuit_breaker as module

        recorded: list[tuple[str, int]] = []
        monkeypatch.setattr(module, "record_circuit_breaker_state", lambda n, s: recorded.append((n, s)))

        @with_circuit_breaker("test-state-export", fail_max=2, timeout_duration=60.0)
        async def always_fail():
            raise ConnectionError("down")

        # Drive the real breaker rather than calling the listener by hand: the
        # listener is wired in get_breaker, and only a real transition proves it.
        with pytest.raises(ConnectionError):
            await always_fail()
        with pytest.raises(CircuitBreakerOpenError):
            await always_fail()

        assert ("test-state-export", 1) in recorded, f"open state was not exported: {recorded}"

    @pytest.mark.asyncio
    async def test_recovery_records_a_non_open_state(self, monkeypatch):
        """A breaker that opens and never reports closing would leave the alert
        firing forever, so the closing transition must be exported too."""
        import services.inference._circuit_breaker as module

        recorded: list[tuple[str, int]] = []
        monkeypatch.setattr(module, "record_circuit_breaker_state", lambda n, s: recorded.append((n, s)))

        breaker = get_breaker("test-state-recovery", fail_max=2, timeout_duration=60.0)
        breaker.open()
        breaker.close()

        states = [s for n, s in recorded if n == "test-state-recovery"]
        assert 1 in states, f"open not exported: {recorded}"
        assert 0 in states, f"closed not exported: {recorded}"


def test_a_new_breaker_publishes_closed_before_any_transition(monkeypatch):
    """State is otherwise written only on a transition, so a breaker that never
    tripped had no series and the dashboard read "Unknown" on a healthy system."""
    import services.inference._circuit_breaker as module

    recorded: list[tuple[str, int]] = []
    monkeypatch.setattr(module, "record_circuit_breaker_state", lambda n, s: recorded.append((n, s)))

    module.get_breaker("fresh-breaker")
    module.get_breaker("fresh-breaker")

    assert recorded == [("fresh-breaker", 0)]


def test_the_closed_state_is_written_before_the_breaker_is_reachable(monkeypatch):
    """Written after registration, the initial "closed" could land after
    another caller had already opened the breaker, and overwrite its exported
    "open": ``OpenRagCircuitBreakerOpen`` would stay silent until the next
    transition."""
    import services.inference._circuit_breaker as module

    reachable_at_write: list[bool] = []
    monkeypatch.setattr(
        module, "record_circuit_breaker_state", lambda n, s: reachable_at_write.append(n in module._breakers)
    )

    module.get_breaker("ordered-breaker")

    assert reachable_at_write == [False]


@pytest.mark.parametrize("breaker", ["embedder", "reranker", "vlm"])
@pytest.mark.parametrize(
    ("status", "excluded"),
    [(400, True), (403, True), (404, True), (408, True), (429, True), (401, False), (500, False)],
)
def test_a_refused_credential_counts_toward_the_breaker(breaker: str, status: int, excluded: bool) -> None:
    """A revoked or wrong key (401) fails every call alike. Excluded like any
    4xx, it could never open the breaker, and the error-ratio alert ignored it
    too. 403 stays excluded: it can be one request's model the key may not use,
    and counting it would let a user open the shared breaker for everyone. Both
    shapes the breaker sees are checked: the raw httpx error and the wrapped one."""
    from core.utils.exceptions import InferenceError
    from services.inference import _circuit_breaker as cb

    request = httpx.Request("POST", "http://provider.invalid/v1/embeddings")
    raw = httpx.HTTPStatusError("refused", request=request, response=httpx.Response(status, request=request))

    assert cb._is_excluded(raw, breaker=breaker) is excluded
    assert cb._is_excluded(InferenceError("refused", status_code=status), breaker=breaker) is excluded


@pytest.mark.parametrize(
    ("status", "excluded"),
    [(400, True), (401, True), (403, True), (408, True), (429, True), (500, False)],
)
def test_the_llm_breaker_excludes_every_4xx(status: int, excluded: bool) -> None:
    """Callers shape the LLM's request: the model through llm_override, and any
    extra chat-body field is forwarded. LiteLLM answers a refused model, or an
    ``api_key`` sent in the body, with 401, so a counted 401 let one burst of
    such requests open the shared breaker for every tenant."""
    from core.utils.exceptions import InferenceError
    from services.inference import _circuit_breaker as cb

    assert cb._is_excluded(InferenceError("refused", status_code=status), breaker="llm") is excluded


def test_each_breaker_applies_its_own_401_rule() -> None:
    """The rule is bound when the breaker is built, from its name."""
    from core.utils.exceptions import InferenceError
    from services.inference import _circuit_breaker as cb

    refused = InferenceError("refused", status_code=401)

    assert cb.get_breaker("llm").is_system_error(refused) is False
    assert cb.get_breaker("embedder").is_system_error(refused) is True
