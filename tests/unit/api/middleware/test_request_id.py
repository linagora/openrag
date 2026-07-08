"""Tests for :class:`api.middleware.request_id.RequestIdMiddleware`.

Covers both responsibilities that share this middleware slot per the
Phase 10C plan: per-request id generation/echo, and the token-redaction
behaviour merged in from the legacy ``TokenRedactingMiddleware``.
"""

from __future__ import annotations

import re

import pytest
from api.middleware.request_id import REQUEST_ID_HEADER, RequestIdMiddleware
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient


@pytest.fixture()
def app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)

    @app.get("/echo")
    async def echo(request: Request) -> dict[str, object]:
        # Expose state to assertions — the middleware should have
        # populated request_id (and, when ?token= was present,
        # original_token + redacted query string).
        return {
            "request_id": request.state.request_id,
            "original_token": getattr(request.state, "original_token", None),
            "query_string": request.scope.get("query_string", b"").decode(),
        }

    return app


@pytest.fixture()
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------------------
# Request id
# ---------------------------------------------------------------------------


def test_generates_request_id_when_header_absent(client: TestClient) -> None:
    """A bare request with no X-Request-ID gets a fresh ``req_<hex>``
    value set on ``request.state`` and echoed on the response."""
    response = client.get("/echo")
    assert response.status_code == 200
    request_id = response.json()["request_id"]
    assert re.fullmatch(r"req_[0-9a-f]{32}", request_id)
    assert response.headers[REQUEST_ID_HEADER] == request_id


def test_honours_inbound_request_id_header(client: TestClient) -> None:
    """An incoming X-Request-ID is preserved unchanged so a gateway id
    can propagate end-to-end through OpenRAG."""
    response = client.get("/echo", headers={REQUEST_ID_HEADER: "trace-abc-123"})
    assert response.status_code == 200
    assert response.json()["request_id"] == "trace-abc-123"
    assert response.headers[REQUEST_ID_HEADER] == "trace-abc-123"


def test_distinct_requests_get_distinct_ids(client: TestClient) -> None:
    """Two unprovenanced requests must not share the same generated id —
    otherwise log correlation collapses to a single bucket."""
    a = client.get("/echo").json()["request_id"]
    b = client.get("/echo").json()["request_id"]
    assert a != b


# ---------------------------------------------------------------------------
# Token redaction (merged TokenRedactingMiddleware behaviour)
# ---------------------------------------------------------------------------


def test_token_query_param_is_redacted_in_scope(client: TestClient) -> None:
    """``?token=…`` is rewritten to ``?token=[REDACTED]`` in the request
    scope so it cannot reach access logs, while the original value is
    preserved on ``request.state.original_token`` for AuthMiddleware."""
    response = client.get("/echo?token=s3cr3t-value&foo=bar")
    assert response.status_code == 200
    body = response.json()
    assert body["original_token"] == "s3cr3t-value"
    # The remaining query string still carries ``foo=bar`` but ``token``
    # is masked.
    assert "token=%5BREDACTED%5D" in body["query_string"] or "token=[REDACTED]" in body["query_string"]
    assert "foo=bar" in body["query_string"]
    assert "s3cr3t-value" not in body["query_string"]


def test_no_token_param_leaves_query_string_untouched(client: TestClient) -> None:
    """Requests without ``?token=`` are not modified."""
    response = client.get("/echo?foo=bar&baz=qux")
    body = response.json()
    assert body["original_token"] is None
    assert body["query_string"] == "foo=bar&baz=qux"


def test_uppercase_token_param_is_masked_but_not_preserved(client: TestClient) -> None:
    """Pre-existing asymmetry inherited verbatim from the legacy
    ``TokenRedactingMiddleware``: the redaction regex is
    ``re.IGNORECASE`` so ``?Token=`` is masked in the log-visible query
    string, but the ``original_token`` capture uses ``parse_qs`` which
    is case-sensitive — only lowercase ``?token=`` round-trips to
    ``request.state.original_token``. Locked in here so a future fix
    is an intentional change, not an accidental regression."""
    response = client.get("/echo?Token=upper")
    body = response.json()
    assert body["original_token"] is None
    assert "upper" not in body["query_string"]


# ---------------------------------------------------------------------------
# Stack-position contract (request_id available to downstream code)
# ---------------------------------------------------------------------------


def test_request_id_is_visible_to_route_handlers() -> None:
    """The id is populated on ``request.state`` before the route runs,
    so handlers + nested middleware can use it for log binding without
    re-deriving it."""
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)

    captured: dict[str, str | None] = {}

    @app.get("/capture")
    async def capture(request: Request) -> dict[str, str]:
        captured["value"] = getattr(request.state, "request_id", None)
        return {"ok": "1"}

    client = TestClient(app)
    client.get("/capture", headers={REQUEST_ID_HEADER: "from-handler"})
    assert captured["value"] == "from-handler"
