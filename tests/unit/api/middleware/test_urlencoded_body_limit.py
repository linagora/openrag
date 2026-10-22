"""Tests for the URL-encoded request body limit."""

import pathlib

import pytest
from api.middleware.urlencoded_body_limit import (
    MAX_URLENCODED_BODY_BYTES,
    MAX_URLENCODED_FIELDS,
    UrlencodedBodyLimitMiddleware,
)
from fastapi import FastAPI
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

URLENCODED = "application/x-www-form-urlencoded"


def _client() -> tuple[TestClient, list[str]]:
    calls: list[str] = []

    async def form(request: Request):
        calls.append("form")
        parsed = await request.form()
        return JSONResponse({key: str(value) for key, value in parsed.items()})

    async def body(request: Request):
        calls.append("body")
        return JSONResponse({"length": len(await request.body())})

    app = Starlette(routes=[Route("/form", form, methods=["POST"]), Route("/body", body, methods=["POST"])])
    app.add_middleware(UrlencodedBodyLimitMiddleware)
    return TestClient(app), calls


def _fields(count: int) -> bytes:
    return "&".join(f"f{i}=v" for i in range(count)).encode()


def test_a_small_form_reaches_the_route_unchanged():
    client, calls = _client()
    response = client.post("/form", content=b"logout_token=abc.def.ghi&x=1", headers={"content-type": URLENCODED})
    assert response.status_code == 200
    assert response.json() == {"logout_token": "abc.def.ghi", "x": "1"}
    assert calls == ["form"]


async def _drive(chunks: list[bytes], headers: list[tuple[bytes, bytes]]) -> tuple[int, list[dict]]:
    """Run the middleware over a body sent as one ASGI message per chunk, without
    a content-length, the way a chunked upload arrives. TestClient would send it
    as a single message."""
    messages = [{"type": "http.request", "body": chunk, "more_body": True} for chunk in chunks]
    messages.append({"type": "http.request", "body": b"", "more_body": False})
    seen: list[dict] = []

    async def inner(scope, receive, send):
        parsed = await Request(scope, receive).form()
        seen.append({key: str(value) for key, value in parsed.items()})
        await JSONResponse({})(scope, receive, send)

    async def receive():
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/form", "headers": headers, "query_string": b""}
    await UrlencodedBodyLimitMiddleware(inner)(scope, receive, send)
    return sent[0]["status"], seen


@pytest.mark.asyncio
async def test_a_form_sent_in_chunks_is_reassembled():
    status, seen = await _drive([b"logout_token=", b"abc", b".def&x=1"], [(b"content-type", URLENCODED.encode())])
    assert status == 200
    assert seen == [{"logout_token": "abc.def", "x": "1"}]


def test_a_declared_length_over_the_limit_is_refused_before_the_route():
    client, calls = _client()
    body = b"a=" + b"x" * MAX_URLENCODED_BODY_BYTES
    response = client.post("/form", content=body, headers={"content-type": URLENCODED})
    assert response.status_code == 413
    assert response.json()["detail"] == "Form body too large."
    assert calls == []


@pytest.mark.asyncio
async def test_a_chunked_body_over_the_limit_is_refused_before_the_route():
    chunk = b"x" * 8192
    chunks = [b"a="] + [chunk] * (MAX_URLENCODED_BODY_BYTES // len(chunk) + 1)
    status, seen = await _drive(chunks, [(b"content-type", URLENCODED.encode())])
    assert status == 413
    assert seen == []


def test_the_field_limit_is_inclusive():
    client, calls = _client()
    response = client.post("/form", content=_fields(MAX_URLENCODED_FIELDS), headers={"content-type": URLENCODED})
    assert response.status_code == 200
    assert len(response.json()) == MAX_URLENCODED_FIELDS


def test_too_many_fields_are_refused_before_the_route():
    client, calls = _client()
    response = client.post("/form", content=_fields(MAX_URLENCODED_FIELDS + 1), headers={"content-type": URLENCODED})
    assert response.status_code == 413
    assert response.json()["detail"] == "Form has too many fields."
    assert calls == []


@pytest.mark.parametrize(
    "content_type",
    [
        "application/x-www-form-urlencoded; charset=UTF-8",
        "Application/X-WWW-Form-Urlencoded",
        "  application/x-www-form-urlencoded  ",
    ],
)
def test_content_type_variants_are_bounded(content_type):
    client, calls = _client()
    response = client.post("/form", content=_fields(MAX_URLENCODED_FIELDS + 1), headers={"content-type": content_type})
    assert response.status_code == 413
    assert calls == []


def test_a_large_multipart_upload_is_not_touched():
    client, calls = _client()
    payload = b"x" * (4 * MAX_URLENCODED_BODY_BYTES)
    response = client.post("/body", files={"file": ("doc.txt", payload, "text/plain")})
    assert response.status_code == 200
    assert response.json()["length"] > len(payload)
    assert calls == ["body"]


def test_a_large_json_body_is_not_touched():
    client, calls = _client()
    body = b'{"a": "' + b"x" * (4 * MAX_URLENCODED_BODY_BYTES) + b'"}'
    response = client.post("/body", content=body, headers={"content-type": "application/json"})
    assert response.status_code == 200
    assert response.json() == {"length": len(body)}


def test_the_backchannel_logout_route_refuses_a_large_form_before_its_mode_check(monkeypatch):
    """FastAPI parses the route's form before its `_require_oidc_mode` dependency,
    so without the middleware this body would be parsed in full, then refused."""
    monkeypatch.setenv("AUTH_MODE", "token")
    from api.routers.auth.oidc import router

    app = FastAPI()
    app.include_router(router)
    app.add_middleware(UrlencodedBodyLimitMiddleware)
    client = TestClient(app)

    small = client.post("/auth/backchannel-logout", data={"logout_token": "lt"})
    assert small.status_code == 400
    assert "AUTH_MODE is not 'oidc'" in small.json()["detail"]

    large = client.post("/auth/backchannel-logout", content=_fields(1_000_000), headers={"content-type": URLENCODED})
    assert large.status_code == 413


def test_main_registers_the_limit_before_auth():
    # Read as text rather than importing: importing `api.main` pulls in the
    # chainlit entrypoint, which fails outside a running app.
    source = (pathlib.Path(__file__).resolve().parents[4] / "openrag" / "api" / "main.py").read_text(encoding="utf-8")
    auth = source.index("app.add_middleware(\n    AuthMiddleware,")
    limit = source.index("app.add_middleware(UrlencodedBodyLimitMiddleware)")
    request_id = source.index("app.add_middleware(RequestIdMiddleware)")
    # Registration is the reverse of execution: registered after Auth, it runs before it.
    assert auth < limit < request_id
