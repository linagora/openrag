"""Attach conservative security headers to every response.

Sets baseline hardening headers app-wide:

- ``X-Content-Type-Options: nosniff`` — stop browser MIME-sniffing.
- ``X-Frame-Options: SAMEORIGIN`` — clickjacking protection. ``SAMEORIGIN``
  (not ``DENY``) so same-origin embedding of the mounted UIs still works.
- ``Referrer-Policy: strict-origin-when-cross-origin`` — don't leak full URLs
  (which can carry a ``?token=``) to third-party origins.
- ``Strict-Transport-Security`` — sent only when the request is already HTTPS
  (browsers ignore it over plain HTTP anyway), so local HTTP development is
  unaffected.

A full Content-Security-Policy is intentionally omitted: the mounted admin UI
and Chainlit rely on inline scripts/styles, so a blanket script CSP would break
them and needs per-UI tuning. Frame protection is covered by X-Frame-Options.

``setdefault`` is used so a route that sets a stricter value of its own (e.g.
the download route's ``X-Content-Type-Options``) is preserved.
"""

from __future__ import annotations

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

_HSTS_VALUE = "max-age=63072000; includeSubDomains"


def _is_https(request: Request) -> bool:
    if request.url.scheme == "https":
        return True
    xfp = request.headers.get("x-forwarded-proto", "")
    return xfp.split(",", 1)[0].strip().lower() == "https"


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add baseline security response headers to every response."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        headers = response.headers
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        if _is_https(request):
            headers.setdefault("Strict-Transport-Security", _HSTS_VALUE)
        return response
