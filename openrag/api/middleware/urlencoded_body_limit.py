"""Bound URL-encoded request bodies before anything parses them.

Starlette's ``application/x-www-form-urlencoded`` parser takes no field-count or
size limit, unlike its multipart parser, and FastAPI parses a route's form before
running its dependencies.

This middleware reads such a body itself, up to a byte cap and
:data:`MAX_URLENCODED_FIELDS`, and answers 413 beyond either. A body within both is
handed on unchanged. The field count is what bounds the parser's cost; the byte cap
only bounds memory. For an authenticated user it is
:data:`MAX_URLENCODED_BODY_BYTES`, Starlette's multipart part limit, which a
metadata update sent as a form can approach once percent-encoded. A request with no
user gets :data:`MAX_ANONYMOUS_URLENCODED_BODY_BYTES`. Uploads are multipart, which
Starlette already bounds, and are not touched.

Register it inside Auth: a request Auth refuses is then answered before its body is
read, a route Auth lets through without credentials still has its form bounded
before FastAPI parses it, and the user Auth found decides the byte cap.
"""

from __future__ import annotations

from python_multipart.multipart import parse_options_header
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

#: Largest URL-encoded body accepted from an authenticated user, Starlette's
#: ``max_part_size`` for multipart.
MAX_URLENCODED_BODY_BYTES = 1024 * 1024

#: Largest URL-encoded body accepted on a request Auth let through without a user:
#: a logout token, Chainlit's login form. It is uvicorn's ``HIGH_WATER_LIMIT``, the
#: point at which uvicorn stops reading a body nobody consumes, so a client that
#: stalls mid-body pins about what uvicorn already holds for a stalled connection.
MAX_ANONYMOUS_URLENCODED_BODY_BYTES = 64 * 1024

#: Most fields accepted in a URL-encoded body. Chainlit's login form has six.
MAX_URLENCODED_FIELDS = 64

_URLENCODED = b"application/x-www-form-urlencoded"


def _is_urlencoded(headers: Headers) -> bool:
    # Starlette parses a URL-encoded form when python-multipart's
    # parse_options_header returns this media type, so decide with the same
    # function: its trimming strips more than ``bytes.strip`` does. Ignoring case
    # also bounds a few headers Starlette would leave unparsed, which is harmless.
    media_type, _ = parse_options_header(headers.get("content-type"))
    return media_type.lower() == _URLENCODED


def _body_limit(scope: Scope) -> int:
    # Auth stores the user it authenticated in the request state, which Starlette
    # keeps in the scope; a route Auth bypasses has none.
    if scope.get("state", {}).get("user") is None:
        return MAX_ANONYMOUS_URLENCODED_BODY_BYTES
    return MAX_URLENCODED_BODY_BYTES


def _declared_length(headers: Headers) -> int | None:
    value = headers.get("content-length")
    if value is None or not value.isdigit():
        return None
    return int(value)


class UrlencodedBodyLimitMiddleware:
    """Answer 413 to a URL-encoded body over the size or field-count limit."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        if not _is_urlencoded(headers):
            await self.app(scope, receive, send)
            return

        limit = _body_limit(scope)
        declared = _declared_length(headers)
        if declared is not None and declared > limit:
            await _too_large(scope, receive, send, "Form body too large.")
            return

        body = bytearray()
        disconnect: Message | None = None
        more_body = True
        while more_body:
            message = await receive()
            if message["type"] == "http.disconnect":
                disconnect = message
                break
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > limit:
                await _too_large(scope, receive, send, "Form body too large.")
                return
            body += chunk
            more_body = message.get("more_body", False)

        buffered: list[Message]
        if disconnect is not None:
            # The client left before the last chunk. Hand on only the disconnect,
            # so the route sees an unfinished request, as it would without this
            # middleware, and never a truncated form.
            buffered = [disconnect]
        else:
            # Fields are separated by "&", the only separator the parser splits on
            # from python-multipart 0.0.30, the floor in pyproject.toml.
            if body.count(b"&") + 1 > MAX_URLENCODED_FIELDS:
                await _too_large(scope, receive, send, "Form has too many fields.")
                return
            buffered = [{"type": "http.request", "body": bytes(body), "more_body": False}]

        async def replay() -> Message:
            if buffered:
                return buffered.pop(0)
            return await receive()

        await self.app(scope, replay, send)


async def _too_large(scope: Scope, receive: Receive, send: Send, detail: str) -> None:
    response = JSONResponse({"detail": detail, "extra": {}}, status_code=413)
    await response(scope, receive, send)


__all__ = [
    "MAX_ANONYMOUS_URLENCODED_BODY_BYTES",
    "MAX_URLENCODED_BODY_BYTES",
    "MAX_URLENCODED_FIELDS",
    "UrlencodedBodyLimitMiddleware",
]
