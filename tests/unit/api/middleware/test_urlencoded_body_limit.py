"""Tests for the URL-encoded request body limit."""

import asyncio
import importlib
import pathlib
import sys
import types

import pytest
from api.middleware.auth import AuthMiddleware
from api.middleware.urlencoded_body_limit import (
    MAX_ANONYMOUS_URLENCODED_BODY_BYTES,
    MAX_URLENCODED_BODY_BYTES,
    MAX_URLENCODED_FIELDS,
    UrlencodedBodyLimitMiddleware,
)
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from starlette.middleware.exceptions import ExceptionMiddleware
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route
from starlette.testclient import TestClient

URLENCODED = "application/x-www-form-urlencoded"

#: A user as Auth leaves it in the request state; only its presence matters here.
USER = {"id": 2}

#: Each byte cap with the request it applies to.
LIMITS = [
    pytest.param(None, MAX_ANONYMOUS_URLENCODED_BODY_BYTES, id="anonymous"),
    pytest.param(USER, MAX_URLENCODED_BODY_BYTES, id="authenticated"),
]


def _client(user: dict | None = None) -> tuple[TestClient, list[str]]:
    # A route is recorded once it has read its body, so an empty list means the
    # body was refused before the route got it.
    calls: list[str] = []

    async def form(request: Request):
        parsed = await request.form()
        calls.append("form")
        return JSONResponse({key: str(value) for key, value in parsed.items()})

    async def body(request: Request):
        length = len(await request.body())
        calls.append("body")
        return JSONResponse({"length": length})

    app = FastAPI(routes=[Route("/form", form, methods=["POST"]), Route("/body", body, methods=["POST"])])
    app.add_middleware(UrlencodedBodyLimitMiddleware)
    if user is None:
        return TestClient(app), calls

    async def as_user(scope, receive, send):
        scope.setdefault("state", {})["user"] = user
        await app(scope, receive, send)

    return TestClient(as_user), calls


def _fields(count: int) -> bytes:
    return "&".join(f"f{i}=v" for i in range(count)).encode()


def test_a_small_form_reaches_the_route_unchanged():
    client, calls = _client()
    response = client.post("/form", content=b"logout_token=abc.def.ghi&x=1", headers={"content-type": URLENCODED})
    assert response.status_code == 200
    assert response.json() == {"logout_token": "abc.def.ghi", "x": "1"}
    assert calls == ["form"]


async def _drive(
    chunks: list[bytes], headers: list[tuple[bytes, bytes]], *, complete: bool = True, user: dict | None = None
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
    if user is not None:
        scope["state"] = {"user": user}
    # The router sits behind Starlette's ExceptionMiddleware, which answers the 413.
    await UrlencodedBodyLimitMiddleware(ExceptionMiddleware(inner))(scope, receive, send)
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


@pytest.mark.asyncio
async def test_a_route_that_ignores_the_body_answers_without_waiting_for_it():
    """An incomplete URL-encoded body sent to a route that never reads it, such as
    a GET on /health_check, must not hold the request open."""
    never_sent = asyncio.Event()

    async def health(scope, receive, send):
        await JSONResponse({"status": "ok"})(scope, receive, send)

    async def receive():
        await never_sent.wait()

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    headers = [(b"content-type", URLENCODED.encode()), (b"content-length", b"1000")]
    scope = {"type": "http", "method": "GET", "path": "/health_check", "headers": headers, "query_string": b""}
    await asyncio.wait_for(UrlencodedBodyLimitMiddleware(health)(scope, receive, send), timeout=1)
    assert sent[0]["status"] == 200


@pytest.mark.parametrize(
    "length",
    [
        pytest.param(str(10 * MAX_URLENCODED_BODY_BYTES).encode(), id="declared-too-large"),
        pytest.param(None, id="chunked"),
    ],
)
@pytest.mark.asyncio
async def test_a_streaming_route_that_ignores_the_body_streams_to_the_end(length):
    """Under ASGI spec 2.3, as uvicorn advertises, StreamingResponse reads receive()
    for a disconnect while it streams. Body data reaching that listener, however
    large, must not raise the 413 and cut the stream."""

    async def chunks():
        for i in range(3):
            yield f"chunk{i}"
            await asyncio.sleep(0.01)

    async def stream(scope, receive, send):
        await StreamingResponse(chunks())(scope, receive, send)

    finished = asyncio.Event()
    messages = [{"type": "http.request", "body": _fields(MAX_URLENCODED_FIELDS + 1), "more_body": False}]

    async def receive():
        await asyncio.sleep(0.005)
        if messages:
            return messages.pop(0)
        await finished.wait()
        return {"type": "http.disconnect"}

    sent: list[dict] = []

    async def send(message):
        sent.append(message)
        if message["type"] == "http.response.body" and not message.get("more_body", False):
            finished.set()

    headers = [(b"content-type", URLENCODED.encode())]
    if length is not None:
        headers.append((b"content-length", length))
    scope = {
        "type": "http",
        "asgi": {"spec_version": "2.3"},
        "method": "GET",
        "path": "/stream",
        "headers": headers,
        "query_string": b"",
    }
    await asyncio.wait_for(UrlencodedBodyLimitMiddleware(stream)(scope, receive, send), timeout=2)
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    assert sent[0]["status"] == 200
    assert body == b"chunk0chunk1chunk2"


@pytest.mark.parametrize(("user", "limit"), LIMITS)
def test_the_byte_limit_is_inclusive(user, limit):
    client, calls = _client(user)
    body = b"a=" + b"x" * (limit - 2)
    response = client.post("/form", content=body, headers={"content-type": URLENCODED})
    assert response.status_code == 200
    assert calls == ["form"]


@pytest.mark.parametrize(("user", "limit"), LIMITS)
def test_a_declared_length_over_the_limit_is_refused_before_the_route(user, limit):
    client, calls = _client(user)
    body = b"a=" + b"x" * (limit - 1)
    response = client.post("/form", content=body, headers={"content-type": URLENCODED})
    assert response.status_code == 413
    assert response.json()["detail"] == "Form body too large."
    assert calls == []


@pytest.mark.parametrize(("user", "limit"), LIMITS)
@pytest.mark.asyncio
async def test_a_chunked_body_over_the_limit_is_refused_before_the_route(user, limit):
    chunk = b"x" * 8192
    chunks = [b"a="] + [chunk] * (limit // len(chunk))
    status, seen = await _drive(chunks, [(b"content-type", URLENCODED.encode())], user=user)
    assert status == 413
    assert seen == []


@pytest.mark.parametrize(("user", "limit"), LIMITS)
@pytest.mark.asyncio
async def test_a_declared_length_over_the_limit_is_refused_at_the_first_chunk(user, limit):
    # The declared length is checked on the first chunk read, so the rest of the
    # body is never read, however little of it has arrived.
    headers = [(b"content-type", URLENCODED.encode()), (b"content-length", str(limit + 1).encode())]
    status, seen = await _drive([b"a=1"], headers, user=user)
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


def test_a_large_form_with_few_fields_reaches_the_route_for_a_user():
    # A metadata update sent as a form: percent-encoding inflates its JSON.
    client, calls = _client(USER)
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

    app = FastAPI(
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


class _DevAuthService:
    """What Auth asks for when ALLOW_NO_AUTH lets every request in as the admin."""

    async def get_user_for_request(self, user_id):
        return {"id": user_id, "is_admin": True}

    async def list_user_partitions_for_request(self, user_id):
        return []


def test_the_user_auth_found_decides_the_byte_cap(monkeypatch):
    """Auth keeps its user in the request state, which the limit reads from the scope."""
    monkeypatch.setenv("AUTH_MODE", "token")
    monkeypatch.delenv("AUTH_TOKEN", raising=False)
    monkeypatch.setenv("ALLOW_NO_AUTH", "true")

    async def form(request: Request):
        return JSONResponse({"fields": len(await request.form())})

    app = FastAPI(routes=[Route("/v1/form", form, methods=["POST"])])
    app.add_middleware(UrlencodedBodyLimitMiddleware)
    app.add_middleware(AuthMiddleware, get_auth_service=lambda request: _DevAuthService())
    body = b"a=" + b"x" * MAX_ANONYMOUS_URLENCODED_BODY_BYTES

    response = TestClient(app).post("/v1/form", content=body, headers={"content-type": URLENCODED})

    assert response.status_code == 200


def test_a_route_auth_lets_through_gets_the_anonymous_byte_cap(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "token")
    monkeypatch.setenv("AUTH_TOKEN", "secret")
    body = b"logout_token=" + b"x" * MAX_ANONYMOUS_URLENCODED_BODY_BYTES
    response = _auth_stack().post("/auth/backchannel-logout", content=body, headers={"content-type": URLENCODED})
    assert response.status_code == 413
    assert response.json()["detail"] == "Form body too large."


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
