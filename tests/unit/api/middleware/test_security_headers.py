"""Tests for the baseline security-headers middleware."""

from api.middleware.security_headers import SecurityHeadersMiddleware
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient


def _app():
    async def ok(_request: Request):
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/", ok)])
    app.add_middleware(SecurityHeadersMiddleware)
    return TestClient(app)


def test_sets_baseline_headers():
    resp = _app().get("/")
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert resp.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"


def test_no_hsts_over_plain_http():
    # TestClient defaults to http:// — HSTS must not be sent so local HTTP dev
    # (and http health probes) are unaffected.
    resp = _app().get("/")
    assert "Strict-Transport-Security" not in resp.headers


def test_hsts_sent_when_forwarded_proto_https():
    resp = _app().get("/", headers={"X-Forwarded-Proto": "https"})
    assert resp.headers["Strict-Transport-Security"] == "max-age=63072000; includeSubDomains"


def test_does_not_clobber_route_set_header():
    async def strict(_request: Request):
        return PlainTextResponse("x", headers={"X-Frame-Options": "DENY"})

    app = Starlette(routes=[Route("/strict", strict)])
    app.add_middleware(SecurityHeadersMiddleware)
    resp = TestClient(app).get("/strict")
    # setdefault must preserve a stricter value a route chose for itself.
    assert resp.headers["X-Frame-Options"] == "DENY"


def test_unhandled_500_still_gets_headers():
    # Unhandled exceptions are turned into a 500 by Starlette's outer
    # ServerErrorMiddleware, which sits outside the user middleware stack — so
    # the 500 handler must apply the headers itself.
    from api.error_handlers import register_error_handlers

    async def boom(_request: Request):
        raise RuntimeError("boom")

    app = Starlette(routes=[Route("/boom", boom)])
    register_error_handlers(app)
    app.add_middleware(SecurityHeadersMiddleware)

    resp = TestClient(app, raise_server_exceptions=False).get("/boom")
    assert resp.status_code == 500
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "SAMEORIGIN"
