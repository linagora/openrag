"""Tests for the URL-encoded request body limit."""

import importlib
import pathlib
import sys
import types

import pytest
from api.middleware.auth import AuthMiddleware
from api.middleware.urlencoded_body_limit import (
    MAX_URLENCODED_BODY_BYTES,
    MAX_URLENCODED_FIELDS,
    UrlencodedBodyLimitMiddleware,
)
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from starlette.applications import Starlette
from starlette.requests import ClientDisconnect, Request
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


async def _drive(
    chunks: list[bytes], headers: list[tuple[bytes, bytes]], *, complete: bool = True
) -> tuple[int, list[dict]]:
    """Run the middleware over a body sent as one ASGI message per chunk, without
    a content-length, the way a chunked upload arrives. TestClient would send it
    as a single message. With ``complete=False`` the client disconnects before the
    last chunk."""
    messages = [{"type": "http.request", "body": chunk, "more_body": True} for chunk in chunks]
    if complete:
        messages.append({"type": "http.request", "body": b"", "more_body": False})
    seen: list[dict] = []

    async def inner(scope, receive, send):
        try:
            parsed = await Request(scope, receive).form()
        except ClientDisconnect:
            seen.append({"disconnected": "yes"})
            await JSONResponse({})(scope, receive, send)
            return
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


@pytest.mark.asyncio
async def test_a_disconnect_before_the_last_chunk_is_not_replayed_as_a_form():
    status, seen = await _drive([b"logout_token=abc.d"], [(b"content-type", URLENCODED.encode())], complete=False)
    assert status == 200
    assert seen == [{"disconnected": "yes"}]


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


@pytest.mark.parametrize(
    "content_type",
    [
        b"application/x-www-form-urlencoded\xa0",
        b"application/x-www-form-urlencoded\x1f",
        b"\x85application/x-www-form-urlencoded; charset=utf-8",
    ],
)
@pytest.mark.asyncio
async def test_header_bytes_the_parser_trims_do_not_skip_the_limit(content_type):
    """python-multipart trims the header with ``str.strip``, which removes these
    bytes, so Starlette still parses the body as a form; h11 lets them through."""
    status, seen = await _drive([_fields(MAX_URLENCODED_FIELDS + 1)], [(b"content-type", content_type)])
    assert status == 413
    assert seen == []


def test_a_large_form_with_few_fields_reaches_the_route():
    # A metadata update sent as a form: percent-encoding inflates its JSON.
    client, calls = _client()
    metadata = '{"title": "' + "é" * 40_000 + '"}'
    response = client.post("/form", data={"metadata": metadata})
    assert response.status_code == 200
    assert response.json() == {"metadata": metadata}


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


def _auth_stack() -> TestClient:
    """The limit inside Auth, as both apps register it, in token mode."""

    async def form(request: Request):
        return JSONResponse({"fields": len(await request.form())})

    app = Starlette(
        routes=[Route("/v1/form", form, methods=["POST"]), Route("/auth/backchannel-logout", form, methods=["POST"])]
    )
    app.add_middleware(UrlencodedBodyLimitMiddleware)
    app.add_middleware(AuthMiddleware, get_auth_service=lambda request: None)
    return TestClient(app)


def test_auth_refuses_a_protected_route_before_the_body_is_bounded(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "token")
    monkeypatch.setenv("AUTH_TOKEN", "secret")
    response = _auth_stack().post(
        "/v1/form", content=_fields(MAX_URLENCODED_FIELDS + 1), headers={"content-type": URLENCODED}
    )
    assert response.status_code == 403


def test_a_route_auth_lets_through_still_has_its_form_bounded(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "token")
    monkeypatch.setenv("AUTH_TOKEN", "secret")
    response = _auth_stack().post(
        "/auth/backchannel-logout", content=_fields(MAX_URLENCODED_FIELDS + 1), headers={"content-type": URLENCODED}
    )
    assert response.status_code == 413


def test_main_registers_the_limit_inside_auth():
    # Read as text rather than importing: importing `api.main` pulls in the
    # chainlit entrypoint, which fails outside a running app.
    source = (pathlib.Path(__file__).resolve().parents[4] / "openrag" / "api" / "main.py").read_text(encoding="utf-8")
    limit = source.index("app.add_middleware(UrlencodedBodyLimitMiddleware)")
    auth = source.index("app.add_middleware(\n    AuthMiddleware,")
    # Registration is the reverse of execution: registered before Auth, it runs after it.
    assert limit < auth


def test_the_standalone_chainlit_app_bounds_forms(monkeypatch):
    """Ray Serve mode serves ``chainlit_api.app`` on its own port, with its own stack."""
    monkeypatch.setenv("AUTH_MODE", "token")
    monkeypatch.setenv("AUTH_TOKEN", "secret")
    chainlit = types.ModuleType("chainlit")
    chainlit_utils = types.ModuleType("chainlit.utils")

    def mount_chainlit(app: FastAPI, target: str, path: str):
        @app.post(f"{path}/login")
        async def login(request: Request):
            return PlainTextResponse(str(len(await request.form())))

    chainlit_utils.mount_chainlit = mount_chainlit
    chainlit.utils = chainlit_utils
    monkeypatch.setitem(sys.modules, "chainlit", chainlit)
    monkeypatch.setitem(sys.modules, "chainlit.utils", chainlit_utils)
    monkeypatch.delitem(sys.modules, "chainlit_api", raising=False)
    module = importlib.import_module("chainlit_api")

    client = TestClient(module.app)
    small = client.post("/chainlit/login", data={"username": "u", "password": "p"})
    assert small.status_code == 200
    large = client.post(
        "/chainlit/login", content=_fields(MAX_URLENCODED_FIELDS + 1), headers={"content-type": URLENCODED}
    )
    assert large.status_code == 413
