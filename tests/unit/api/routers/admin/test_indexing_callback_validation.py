"""The upload routes must reject an unusable callback_url before queueing."""

import asyncio
import socket
from types import SimpleNamespace

import pytest
from api.routers.admin.indexing import _validate_callback_url
from fastapi import HTTPException


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the suite off real DNS; the resolution tests re-patch it."""
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *a, **k: [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", port))
        ],
    )


def _config(*, allow_private: bool = False) -> SimpleNamespace:
    return SimpleNamespace(indexing_callback=SimpleNamespace(allow_private_urls=allow_private))


@pytest.mark.asyncio
async def test_tokenized_http_callback_is_rejected() -> None:
    with pytest.raises(HTTPException) as exc:
        await _validate_callback_url("http://cozy.example.com/ai/index/status", "jwt", _config())

    assert exc.value.status_code == 400
    assert "https" in exc.value.detail


@pytest.mark.asyncio
async def test_tokenized_https_callback_is_accepted() -> None:
    await _validate_callback_url("https://cozy.example.com/ai/index/status", "jwt", _config())


@pytest.mark.asyncio
async def test_untokenized_http_callback_is_accepted() -> None:
    """Back-compat: only token-bearing callbacks gain the https requirement."""
    await _validate_callback_url("http://cozy.example.com/rag/callback", None, _config())


@pytest.mark.asyncio
async def test_tokenized_http_callback_is_accepted_under_the_dev_opt_in() -> None:
    await _validate_callback_url("http://cozy.localhost:8080/ai/index/status", "jwt", _config(allow_private=True))


@pytest.mark.asyncio
async def test_private_callback_url_is_rejected_by_default() -> None:
    with pytest.raises(HTTPException) as exc:
        await _validate_callback_url("http://127.0.0.1:8080/cb", None, _config())

    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_no_callback_url_is_accepted_even_with_a_token() -> None:
    """A token without a URL is inert — the sender is a strict no-op."""
    await _validate_callback_url(None, "jwt", _config())


@pytest.mark.asyncio
async def test_hostname_resolving_internally_is_rejected(monkeypatch) -> None:
    """Accepting it here queues a job whose callback the sender will drop."""
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("10.0.0.5", port))],
    )

    with pytest.raises(HTTPException) as exc:
        await _validate_callback_url("https://evil.example.com/cb", None, _config())

    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_the_dev_opt_in_skips_resolution(monkeypatch) -> None:
    def _never(*_a, **_k):  # pragma: no cover - must never be called
        raise AssertionError("resolution must be skipped when private URLs are allowed")

    monkeypatch.setattr(socket, "getaddrinfo", _never)

    await _validate_callback_url("http://cozy.localhost:8080/cb", "jwt", _config(allow_private=True))


@pytest.mark.parametrize("callback_url", ["http://[::1/cb", "https://cozy.example.com:abc/cb"])
@pytest.mark.asyncio
async def test_malformed_callback_url_is_a_bad_request_not_a_crash(callback_url: str) -> None:
    """urlparse and .port both raise ValueError; unguarded that is a 500."""
    with pytest.raises(HTTPException) as exc:
        await _validate_callback_url(callback_url, None, _config())

    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_a_hanging_resolver_is_rejected_rather_than_holding_the_request(monkeypatch) -> None:
    """getaddrinfo runs on the shared default executor; an unbounded wait starves it."""
    import api.routers.admin.indexing as module

    monkeypatch.setattr(module, "_CALLBACK_DNS_TIMEOUT", 0.01)

    async def _never_resolves(*_a, **_k):
        await asyncio.sleep(5)
        return True

    monkeypatch.setattr(module, "resolves_to_public_addresses", _never_resolves)

    with pytest.raises(HTTPException) as exc:
        await _validate_callback_url("https://slow.example.com/cb", None, _config())

    assert exc.value.status_code == 400
