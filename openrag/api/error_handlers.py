"""Centralised FastAPI exception handlers (Phase 10B).

Moves the inline ``OpenRAGError`` / ``Exception`` handlers out of the
legacy ``openrag/main.py`` into the new ``api/`` namespace. The response
shape is preserved verbatim — existing API consumers, the Robot
Framework suite (``tests/api/``), and several unit tests assert on the
exact ``{"detail": "[CODE]: message", "extra": {...}}`` body — so this
is a pure relocation, not a behaviour change.

Two pieces of forward-looking infrastructure are added:

* :func:`_status_for` walks the exception MRO over :data:`_STATUS_MAP`
  as a safety net for the case where a future ``OpenRAGError`` subclass
  forgets to set its own ``status_code``. Every current subclass in
  ``core/utils/exceptions.py`` already carries one, so the map is
  defensive only.
* :func:`_get_request_id` pulls ``request.state.request_id`` when set
  (Phase 10C's ``RequestIdMiddleware`` populates it) and injects the
  value into ``extra``. The field is omitted until the middleware
  lands, so the response stays byte-identical to legacy behaviour.

:func:`register_error_handlers` is the single attachment point called
from ``api/main.py``.
"""

from __future__ import annotations

from core.utils.exceptions import (
    AuthenticationError,
    AuthError,
    InferenceError,
    InferenceTimeoutError,
    LLMParsingError,
    NotFoundError,
    OpenRAGError,
    QuotaExceededError,
    ServiceUnavailableError,
    StorageError,
    ValidationError,
)
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from utils.logger import get_logger

logger = get_logger()


# MRO walk fallback. Order matters — Python checks ``type(exc).__mro__``
# in inheritance order, so the most specific class must come first for
# the look-up to return the tightest match (``AuthenticationError``
# before ``AuthError``, ``InferenceTimeoutError`` / ``LLMParsingError``
# before ``InferenceError``, etc.). Dict iteration order preserves this.
_STATUS_MAP: dict[type[BaseException], int] = {
    AuthenticationError: 401,
    AuthError: 403,
    ValidationError: 422,
    NotFoundError: 404,
    QuotaExceededError: 429,
    InferenceTimeoutError: 504,
    LLMParsingError: 502,
    InferenceError: 503,
    ServiceUnavailableError: 503,
    StorageError: 500,
    OpenRAGError: 500,
}


def _status_for(exc: BaseException) -> int:
    """Return the HTTP status code for ``exc``.

    Prefers ``exc.status_code`` (every ``OpenRAGError`` subclass sets
    one in ``__init__``); falls back to an MRO walk over
    :data:`_STATUS_MAP` for subclasses that omit it. Non-``OpenRAGError``
    inputs (anything caught by the generic handler) default to 500.
    """
    explicit = getattr(exc, "status_code", None)
    if isinstance(explicit, int):
        return explicit
    for cls in type(exc).__mro__:
        if cls in _STATUS_MAP:
            return _STATUS_MAP[cls]
    return 500


def _get_request_id(request: Request) -> str | None:
    """Return the request id set by ``RequestIdMiddleware`` if any.

    Phase 10C's middleware populates ``request.state.request_id``; until
    then this returns ``None`` and the field is omitted from the body so
    the response stays identical to the pre-refactor shape.
    """
    return getattr(request.state, "request_id", None)


async def openrag_exception_handler(request: Request, exc: OpenRAGError) -> JSONResponse:
    """Map an :class:`OpenRAGError` to its declared HTTP status.

    Body matches the legacy ``{"detail": "[CODE]: message", "extra":
    {...}}`` contract. ``extra`` is augmented with ``request_id`` when
    available — additive, so consumers that ignore the field still parse
    the response correctly.
    """
    logger.error(
        "OpenRAGError occurred",
        error_code=getattr(exc, "code", type(exc).__name__),
        status_code=_status_for(exc),
    )
    body = exc.to_dict()
    request_id = _get_request_id(request)
    if request_id is not None:
        # Copy so we never mutate the exception's own ``extra`` dict —
        # the same instance may be re-raised / logged elsewhere.
        body["extra"] = {**body.get("extra", {}), "request_id": request_id}
    return JSONResponse(status_code=_status_for(exc), content=body)


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all 500 handler.

    The body keeps the legacy ``[UNEXPECTED_ERROR]`` prefix so the
    Robot Framework suite and the existing unit assertions still match
    after the move.
    """
    logger.exception("Unhandled exception", error_type=type(exc).__name__)
    extra: dict[str, object] = {}
    request_id = _get_request_id(request)
    if request_id is not None:
        extra["request_id"] = request_id
    return JSONResponse(
        status_code=500,
        content={"detail": "[UNEXPECTED_ERROR]: An unexpected error occurred", "extra": extra},
    )


def register_error_handlers(app: FastAPI) -> None:
    """Attach :func:`openrag_exception_handler` and
    :func:`unhandled_exception_handler` to ``app``.

    Replaces the two inline ``@app.exception_handler`` decorators in the
    legacy ``openrag/main.py`` with a single call from ``api/main.py``.
    """
    app.add_exception_handler(OpenRAGError, openrag_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
