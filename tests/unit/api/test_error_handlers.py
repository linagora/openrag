"""Tests for :mod:`api.error_handlers` (Phase 10B).

Exercises the FastAPI handler wiring end-to-end via ``TestClient`` so a
regression in the response shape (which the Robot Framework suite and
several router tests still assert byte-for-byte) is caught immediately
rather than at integration time.
"""

from __future__ import annotations

import pytest
from api.error_handlers import _STATUS_MAP, _status_for, register_error_handlers
from core.utils.exceptions import (
    ConflictError,
    DocumentNotFoundError,
    InferenceConnectionError,
    InferenceError,
    InferenceTimeoutError,
    NotFoundError,
    OpenRAGError,
    PartitionNotFoundError,
    QuotaExceededError,
    ValidationError,
)
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient


@pytest.fixture()
def app() -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/raise/openrag")
    async def _raise_openrag() -> None:
        raise OpenRAGError("boom", code="BOOM", status_code=418, foo="bar")

    @app.get("/raise/not_found")
    async def _raise_not_found() -> None:
        raise DocumentNotFoundError("missing doc")

    @app.get("/raise/quota")
    async def _raise_quota() -> None:
        raise QuotaExceededError("too many files")

    @app.get("/raise/validation")
    async def _raise_validation() -> None:
        raise ValidationError("bad input")

    @app.get("/raise/conflict")
    async def _raise_conflict() -> None:
        raise ConflictError("already in use")

    @app.get("/raise/inference_connection")
    async def _raise_inference_connection() -> None:
        raise InferenceConnectionError("cannot reach LLM")

    @app.get("/raise/inference_timeout")
    async def _raise_inference_timeout() -> None:
        raise InferenceTimeoutError("LLM timed out")

    @app.get("/raise/inference_upstream")
    async def _raise_inference_upstream() -> None:
        raise InferenceError("LLM error (502): bad gateway", status_code=502)

    @app.get("/raise/unknown")
    async def _raise_unknown() -> None:
        raise RuntimeError("kaboom")

    @app.get("/raise/unknown-with-request-id")
    async def _raise_unknown_with_request_id(request: Request) -> None:
        request.state.request_id = "req_unhandled_1"
        raise RuntimeError("kaboom")

    @app.get("/raise/with-request-id")
    async def _raise_with_request_id(request: Request) -> None:
        # Simulates Phase 10C's RequestIdMiddleware having populated the
        # state before the route ran.
        request.state.request_id = "req_test_123"
        raise NotFoundError("resource gone")

    return app


@pytest.fixture()
def client(app: FastAPI) -> TestClient:
    # raise_server_exceptions=False so the catch-all handler can actually
    # respond instead of TestClient re-raising the RuntimeError.
    return TestClient(app, raise_server_exceptions=False)


def test_openrag_error_uses_declared_status_code(client: TestClient) -> None:
    """OpenRAGError(status_code=418) -> the handler must honour 418."""
    response = client.get("/raise/openrag")
    assert response.status_code == 418
    body = response.json()
    # Legacy contract: top-level {"detail": "[CODE]: message", "extra": {...}}.
    assert body == {"detail": "[BOOM]: boom", "extra": {"foo": "bar"}}


def test_subclass_status_code_is_honored(client: TestClient) -> None:
    """A NotFoundError subclass must map to 404 from its own attribute."""
    response = client.get("/raise/not_found")
    assert response.status_code == 404
    body = response.json()
    assert body["detail"] == "[DOCUMENT_NOT_FOUND]: missing doc"
    assert body["extra"] == {}


def test_quota_exceeded_maps_to_429(client: TestClient) -> None:
    response = client.get("/raise/quota")
    assert response.status_code == 429


def test_validation_error_maps_to_422(client: TestClient) -> None:
    response = client.get("/raise/validation")
    assert response.status_code == 422


def test_conflict_error_maps_to_409(client: TestClient) -> None:
    response = client.get("/raise/conflict")
    assert response.status_code == 409
    body = response.json()
    assert body["detail"] == "[CONFLICT]: already in use"


def test_inference_connection_error_maps_to_503(client: TestClient) -> None:
    """A down resolved LLM endpoint surfaces as 503 through the global handler.

    This is the non-streaming half of the availability-preflight removal's
    error contract: with the default-only pre-flight guard gone, an unreachable
    endpoint is now surfaced by the generation call itself.
    """
    response = client.get("/raise/inference_connection")
    assert response.status_code == 503


def test_inference_timeout_error_maps_to_504(client: TestClient) -> None:
    response = client.get("/raise/inference_timeout")
    assert response.status_code == 504


def test_inference_error_honors_upstream_status_code(client: TestClient) -> None:
    """An upstream HTTP error is passed through with its own status (e.g. 502)."""
    response = client.get("/raise/inference_upstream")
    assert response.status_code == 502


def test_unknown_exception_returns_500_with_legacy_body(client: TestClient) -> None:
    """A bare ``RuntimeError`` hits the catch-all and must produce the
    legacy ``[UNEXPECTED_ERROR]`` body — Robot Framework asserts on it."""
    response = client.get("/raise/unknown")
    assert response.status_code == 500
    body = response.json()
    assert body == {
        "detail": "[UNEXPECTED_ERROR]: An unexpected error occurred",
        "extra": {},
    }


def test_unhandled_exception_log_line_carries_request_id(client: TestClient) -> None:
    """The catch-all runs in Starlette's outermost layer, *after*
    ``RequestIdMiddleware``'s ``contextualize`` scope has unwound, so the id
    must be bound explicitly on the "Unhandled exception" line."""
    from loguru import logger

    captured: list[dict] = []
    handler_id = logger.add(lambda m: captured.append(dict(m.record["extra"])), level="ERROR")
    try:
        response = client.get("/raise/unknown-with-request-id")
    finally:
        logger.remove(handler_id)
    assert response.status_code == 500
    unhandled = [e for e in captured if e.get("error_type") == "RuntimeError"]
    assert len(unhandled) == 1
    assert unhandled[0]["request_id"] == "req_unhandled_1"


def test_request_id_is_injected_into_extra_when_set(client: TestClient) -> None:
    """When RequestIdMiddleware populates ``request.state.request_id`` the
    handler must surface it inside ``extra`` (additive — does not
    replace existing keys)."""
    response = client.get("/raise/with-request-id")
    assert response.status_code == 404
    body = response.json()
    assert body["extra"] == {"request_id": "req_test_123"}
    assert body["detail"] == "[NOT_FOUND]: resource gone"


# ---------------------------------------------------------------------------
# _status_for — unit-level checks (no FastAPI)
# ---------------------------------------------------------------------------


def test_status_for_prefers_explicit_attribute() -> None:
    """``exc.status_code`` wins over the MRO map — that is the path every
    current OpenRAGError subclass takes."""
    exc = PartitionNotFoundError("nope")
    assert exc.status_code == 404
    assert _status_for(exc) == 404


def test_status_for_falls_back_to_mro_walk() -> None:
    """When status_code is absent the MRO walk picks the tightest match
    in ``_STATUS_MAP`` — ``NotFoundError`` here, not ``OpenRAGError``."""

    class StatuslessNotFound(NotFoundError):
        def __init__(self) -> None:
            # Skip the parent __init__ so ``status_code`` is never set.
            Exception.__init__(self, "no status")
            self.message = "no status"
            self.code = "STATUSLESS"
            self.extra: dict[str, object] = {}

    exc = StatuslessNotFound()
    assert getattr(exc, "status_code", None) is None
    assert _status_for(exc) == 404


def test_status_for_defaults_to_500_for_arbitrary_exception() -> None:
    """A non-OpenRAGError exception has no status_code and no MRO match
    — the safety-net default is 500."""
    assert _status_for(RuntimeError("x")) == 500


def test_status_map_lists_specific_classes_first() -> None:
    """Sanity check: the dict ordering relied on by the MRO walk has the
    more specific classes before their bases, so ``AuthenticationError``
    resolves to 401 rather than ``AuthError``'s 403, etc."""
    keys = list(_STATUS_MAP)
    auth_specific = keys.index(
        __import__("core.utils.exceptions", fromlist=["AuthenticationError"]).AuthenticationError
    )
    auth_base = keys.index(__import__("core.utils.exceptions", fromlist=["AuthError"]).AuthError)
    assert auth_specific < auth_base


# ---------------------------------------------------------------------------
# A provider's 401/403 reaches the caller as 502
# ---------------------------------------------------------------------------


@pytest.fixture()
def provider_app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """Routes that call the real reranker and embedder clients against a
    provider answering with the status in the path, so the exception is the
    one production raises, not a hand-built one."""
    import httpx
    from services.inference.reranker_clients import InfinityReranker
    from services.inference.vllm_client import VLLMClient, VLLMEmbedder

    monkeypatch.setenv("LLM_OVERRIDE_ALLOW_CUSTOM_ENDPOINT", "true")
    overrides = {
        "none": {},
        "model": {"model": "another-model"},
        "endpoint": {"base_url": "https://caller-llm.example/v1", "api_key": "caller-key"},
    }

    def transport(status: int) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(status, json={})))

    app = FastAPI()
    register_error_handlers(app)

    @app.get("/rerank/{status}")
    async def _rerank(status: int) -> None:
        reranker = InfinityReranker(endpoint="http://reranker:7997", model_name="m")
        reranker._client = transport(status)
        await reranker.rerank("query", ["doc"])

    @app.get("/embed/{status}")
    async def _embed(status: int) -> None:
        embedder = VLLMEmbedder(endpoint="http://vllm:8000/v1", model_name="m", api_key="k")
        embedder._client = transport(status)
        await embedder.embed(["text"])

    @app.get("/llm/{status}")
    async def _llm(status: int, override: str = "none", op: str = "chat") -> None:
        llm = VLLMClient(endpoint="http://vllm:8000/v1", model_name="m", api_key="k")
        llm._client = transport(status)
        metadata = {"llm_override": overrides[override]}
        if op == "generate":
            await llm.generate("hi", metadata=metadata)
        else:
            await llm.chat([{"role": "user", "content": "hi"}], metadata=metadata)

    @app.get("/auth/{status}")
    async def _auth(status: int) -> None:
        from core.utils.exceptions import AuthenticationError, AuthError

        raise AuthenticationError("bad token") if status == 401 else AuthError("forbidden")

    return app


@pytest.fixture()
def provider_client(provider_app: FastAPI):
    from services.inference._circuit_breaker import _breakers

    yield TestClient(provider_app, raise_server_exceptions=False)
    for breaker in _breakers.values():
        breaker.close()
    _breakers.clear()


@pytest.mark.parametrize("kind", ["rerank", "embed", "llm"])
@pytest.mark.parametrize("status", [401, 403])
def test_a_providers_credential_refusal_is_a_502(provider_client: TestClient, kind: str, status: int) -> None:
    """A provider refusing OpenRag's key is not the caller's token failing. A
    401 passed through told a valid caller it was unauthenticated, and the
    admin UI drops its stored token on any 401."""
    resp = provider_client.get(f"/{kind}/{status}")

    assert resp.status_code == 502
    expected = {
        "rerank": f"returned HTTP {status}",
        "embed": f"Embedder API error ({status})",
        "llm": f"LLM error ({status})",
    }[kind]
    assert expected in resp.json()["detail"]


@pytest.mark.parametrize("op", ["chat", "generate"])
@pytest.mark.parametrize("override", ["model", "endpoint"])
@pytest.mark.parametrize("status", [401, 403])
def test_a_refusal_the_caller_chose_is_their_400(
    provider_client: TestClient, override: str, status: int, op: str
) -> None:
    """``llm_override`` picks the model, or the endpoint and its key: a provider
    refusing either is the caller's request failing, not an upstream fault.
    Still not a 401, which would read as their OpenRag token."""
    resp = provider_client.get(f"/llm/{status}?override={override}&op={op}")

    assert resp.status_code == 400
    assert f"LLM error ({status})" in resp.json()["detail"]


@pytest.mark.parametrize("kind", ["rerank", "embed"])
def test_other_provider_4xx_still_pass_through(provider_client: TestClient, kind: str) -> None:
    """Control: only the credential statuses are remapped."""
    assert provider_client.get(f"/{kind}/400").status_code == 400


@pytest.mark.parametrize("status", [401, 403])
def test_openrags_own_auth_errors_keep_their_status(provider_client: TestClient, status: int) -> None:
    """Control: the caller's own token failing is still a 401/403."""
    assert provider_client.get(f"/auth/{status}").status_code == status


def test_the_exception_keeps_the_providers_status() -> None:
    """Only the response is remapped: the breaker, the retry and the metrics
    read the exception, and must still see the 401."""
    import httpx
    from services.inference.reranker_clients import _raise_reranker_http_error

    request = httpx.Request("POST", "http://reranker/rerank")
    error = httpx.HTTPStatusError("x", request=request, response=httpx.Response(401, request=request))
    with pytest.raises(InferenceConnectionError) as info:
        _raise_reranker_http_error("http://reranker", error)

    assert info.value.status_code == 401
    assert _status_for(info.value) == 502
