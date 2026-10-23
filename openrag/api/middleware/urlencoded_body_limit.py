"""Bound URL-encoded request bodies as they are read.

Starlette's ``application/x-www-form-urlencoded`` parser takes no field-count or
size limit, unlike its multipart parser, and FastAPI parses a route's form before
running its dependencies.

This middleware counts the bytes and fields of such a body as the route reads it,
and raises a 413 :class:`~starlette.exceptions.HTTPException` from ``receive`` once
either passes its limit, before the chunk that crosses it reaches the parser. A
route that never reads the body is not held up waiting for one. FastAPI re-raises
an ``HTTPException`` raised while it reads a body, so the 413 reaches the client.
The field count is what bounds the parser's cost; the byte cap only bounds
memory. For an authenticated user it is
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
from starlette.exceptions import HTTPException
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
        received = 0
        separators = 0
        response_started = False

        async def bounded_receive() -> Message:
            nonlocal received, separators
            message = await receive()
            # Once the response has started, nothing is parsing the body: what reads
            # it is a disconnect listener, such as StreamingResponse's under ASGI
            # spec 2.3, and a 413 can no longer be sent. That listener calls receive
            # before the response starts, so this is checked after the wait.
            if response_started or message["type"] != "http.request":
                return message
            if declared is not None and declared > limit:
                raise HTTPException(status_code=413, detail="Form body too large.")
            chunk = message.get("body", b"")
            received += len(chunk)
            if received > limit:
                raise HTTPException(status_code=413, detail="Form body too large.")
            # Fields are separated by "&", the only separator the parser splits on
            # from python-multipart 0.0.30, the floor in pyproject.toml.
            separators += chunk.count(b"&")
            if separators + 1 > MAX_URLENCODED_FIELDS:
                raise HTTPException(status_code=413, detail="Form has too many fields.")
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        await self.app(scope, bounded_receive, tracking_send)


__all__ = [
    "MAX_ANONYMOUS_URLENCODED_BODY_BYTES",
    "MAX_URLENCODED_BODY_BYTES",
    "MAX_URLENCODED_FIELDS",
    "UrlencodedBodyLimitMiddleware",
]
