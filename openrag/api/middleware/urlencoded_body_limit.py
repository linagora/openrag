"""Bound URL-encoded request bodies before anything parses them.

Starlette's ``application/x-www-form-urlencoded`` parser takes no field-count or
size limit, unlike its multipart parser, and FastAPI parses a route's form before
running its dependencies.

This middleware reads such a body itself, up to :data:`MAX_URLENCODED_BODY_BYTES`
and :data:`MAX_URLENCODED_FIELDS`, and answers 413 beyond either. A body within both
is handed on unchanged. Every legitimate URL-encoded body here is a handful of short
fields (a logout token, a username and password); uploads are multipart, which
Starlette already bounds, and are not touched.
"""

from __future__ import annotations

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

#: Largest URL-encoded body accepted. A back-channel logout token is one signed
#: JWT, a few kilobytes at most.
MAX_URLENCODED_BODY_BYTES = 64 * 1024

#: Most fields accepted in a URL-encoded body. Chainlit's login form has six.
MAX_URLENCODED_FIELDS = 64

_URLENCODED = b"application/x-www-form-urlencoded"


def _header(scope: Scope, name: bytes) -> bytes | None:
    # The first occurrence, as Starlette's ``Headers.get`` returns it.
    for key, value in scope["headers"]:
        if key == name:
            return value
    return None


def _is_urlencoded(scope: Scope) -> bool:
    content_type = _header(scope, b"content-type")
    if content_type is None:
        return False
    # Starlette parses a URL-encoded form when python-multipart's
    # parse_options_header returns this media type. That lowercases it only when
    # the header has no parameters, and otherwise splits at the first ";". Ignoring
    # case here matches every header it accepts, and a few more, which only bounds
    # a body Starlette would have left unparsed.
    media_type = content_type.split(b";", 1)[0].strip().lower()
    return media_type == _URLENCODED


def _declared_length(scope: Scope) -> int | None:
    value = _header(scope, b"content-length")
    if value is None or not value.isdigit():
        return None
    return int(value)


class UrlencodedBodyLimitMiddleware:
    """Answer 413 to a URL-encoded body over the size or field-count limit."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not _is_urlencoded(scope):
            await self.app(scope, receive, send)
            return

        declared = _declared_length(scope)
        if declared is not None and declared > MAX_URLENCODED_BODY_BYTES:
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
            body += message.get("body", b"")
            if len(body) > MAX_URLENCODED_BODY_BYTES:
                await _too_large(scope, receive, send, "Form body too large.")
                return
            more_body = message.get("more_body", False)

        # Fields are separated by "&", the only separator the parser splits on.
        if body.count(b"&") + 1 > MAX_URLENCODED_FIELDS:
            await _too_large(scope, receive, send, "Form has too many fields.")
            return

        buffered: list[Message] = [{"type": "http.request", "body": bytes(body), "more_body": False}]
        if disconnect is not None:
            buffered.append(disconnect)

        async def replay() -> Message:
            if buffered:
                return buffered.pop(0)
            return await receive()

        await self.app(scope, replay, send)


async def _too_large(scope: Scope, receive: Receive, send: Send, detail: str) -> None:
    response = JSONResponse({"detail": detail, "extra": {}}, status_code=413)
    await response(scope, receive, send)


__all__ = ["MAX_URLENCODED_BODY_BYTES", "MAX_URLENCODED_FIELDS", "UrlencodedBodyLimitMiddleware"]
