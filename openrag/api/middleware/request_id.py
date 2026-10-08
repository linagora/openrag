"""RequestIdMiddleware — per-request correlation id + token redaction.

New module in Phase 10C. Two request-scoped responsibilities live here
because both run before any business logic touches the request:

1. **Request id.** Reuses an inbound ``X-Request-ID`` header when one is
   supplied *and well-formed* (so a reverse proxy / gateway id propagates
   end-to-end) and generates a fresh UUID4 otherwise. The value is stored on
   ``request.state.request_id``; :mod:`api.error_handlers` reads it
   from there and surfaces it in the ``extra`` block of every error
   response. The same value is echoed on the response as
   ``X-Request-ID`` so clients can correlate.

2. **Token redaction.** Replaces ``?token=…`` in the query string with
   ``?token=[REDACTED]`` before the access log records it, while
   preserving the original token on ``request.state.original_token`` so
   :class:`AuthMiddleware` can still authenticate ``/static`` URLs that
   pass the credential as a query param. This logic used to live in
   the inline ``TokenRedactingMiddleware`` of the legacy
   ``openrag/main.py``; both responsibilities deal with request-context
   setup so they share the middleware slot per the Phase 10C plan.

3. **Log correlation.** Binds the id into the loguru context for the
   duration of ``call_next`` so every log line of the request carries
   ``request_id``.

Stack position (registered in :mod:`api.main`): inside Instrumentation
and RequestTimeout, outside Auth — so the id is set before
authentication runs (auth failures get a request_id) and metrics
duration covers the full lifetime.
"""

from __future__ import annotations

import re
import uuid
from urllib.parse import parse_qs

from fastapi import Request

# Imported straight from loguru rather than through ``core.utils.logging``:
# ``get_logger()`` re-runs ``logger.remove()`` / ``logger.add()``, so calling it
# here would tear down and reinstall the process sinks on every import order
# change. The bound context is all this module needs.
from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware

REQUEST_ID_HEADER = "X-Request-ID"

# An inbound id is bound onto every log line of the request (and shipped as
# Loki structured metadata), so it is accepted only in the shape gateways
# actually emit: alphanumerics plus ``_ . : -``, bounded length. Anything else
# — whitespace that would forge a field in the text format, control characters,
# a multi-KB value repeated on every line — gets a freshly minted id instead.
# Matched with ``fullmatch``: ``match`` would accept a trailing newline (``$``
# matches before a final one), and a newline is exactly what splits one log
# line into two.
_VALID_REQUEST_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")

# Conservative — matches ``token=<value>`` until the next ``&`` or
# whitespace, regardless of position in the query string. Used for log
# redaction only; the original value is preserved on ``request.state``.
_TOKEN_PATTERN = re.compile(r"(token=)[^&\s]+", re.IGNORECASE)


def _generate_request_id() -> str:
    """Return a new request id.

    UUID4 hex prefixed with ``req_`` so the value is visually
    distinguishable from other ids in logs / dashboards.
    """
    return f"req_{uuid.uuid4().hex}"


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Tag every request with a correlation id and redact query tokens."""

    async def dispatch(self, request: Request, call_next):
        # --- 1) Request id (a well-formed inbound header wins so a gateway id
        # propagates; a malformed one is dropped, not rejected — the request
        # itself is fine, only its correlation id is untrustworthy).
        incoming = request.headers.get(REQUEST_ID_HEADER)
        request_id = incoming if incoming and _VALID_REQUEST_ID.fullmatch(incoming) else _generate_request_id()
        request.state.request_id = request_id

        # --- 2) Strip ``?token=`` from the query string visible to logs,
        # but stash the raw value on ``request.state`` so the ``/static``
        # path of AuthMiddleware can still authenticate it.
        original_query_string = request.scope.get("query_string", b"").decode()
        if "token=" in original_query_string.lower():
            params = parse_qs(original_query_string)
            request.state.original_token = params.get("token", [None])[0]
            redacted = _TOKEN_PATTERN.sub(r"\1[REDACTED]", original_query_string)
            request.scope["query_string"] = redacted.encode()

        # --- 3) Correlate every log of this request. ``contextualize`` is
        # backed by a ContextVar, so the value follows the request across
        # awaits without leaking into concurrent requests — including a
        # streaming body produced after ``call_next`` returns:
        # ``BaseHTTPMiddleware`` runs the downstream app in a task whose
        # context is copied *inside* this block, so the route's generator keeps
        # the binding. What is genuinely outside the scope is anything logged
        # by a middleware registered further out than this one
        # (Instrumentation, RequestTimeout) — those lines carry no request_id.
        with logger.contextualize(request_id=request_id):
            response = await call_next(request)
        # Always echo the id back; clients use it as the correlation key
        # in support tickets and the error-handler ``extra`` field.
        response.headers[REQUEST_ID_HEADER] = request_id
        return response


__all__ = ["RequestIdMiddleware", "REQUEST_ID_HEADER"]
