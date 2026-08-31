"""Tests for the SSRF URL guard."""

from __future__ import annotations

import socket

import pytest
from core.utils.url_safety import (
    RESOLUTION_BLOCKED,
    RESOLUTION_PUBLIC,
    RESOLUTION_RESOLVER_FAILED,
    RESOLUTION_UNKNOWN_HOST,
    is_safe_url,
    resolve_public_addresses,
)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/x",
        "https://localhost/x",
        "http://10.0.0.1/x",
        "http://192.168.1.1/x",
        "http://169.254.169.254/latest/meta-data",  # cloud metadata
        "http://[::1]/x",  # IPv6 loopback
        "http://2130706433/x",  # decimal-encoded 127.0.0.1
        "http://0x7f000001/x",  # hex-encoded 127.0.0.1
        "http://0177.0.0.1/x",  # octal-encoded 127.0.0.1
        "http://127.1/x",  # short form of 127.0.0.1
        "http://0xa9fea9fe/x",  # hex-encoded 169.254.169.254
        "ftp://example.com/x",  # non-http scheme
        "file:///etc/passwd",
        "not a url",
    ],
)
def test_blocks_unsafe(url):
    assert is_safe_url(url) is False


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/doc.pdf",
        "http://files.internal.example.org/a/b.txt",
        "https://8.8.8.8/x",  # public IP literal
    ],
)
def test_allows_public(url):
    assert is_safe_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8080/ai/index/status",
        "http://localhost:8080/ai/index/status",
        "http://cozy.localhost:8080/ai/index/status",
        "http://192.168.1.10:8080/x",
        "http://[::1]:8080/x",
    ],
)
def test_allow_private_hosts_opt_in_permits_private_targets(url):
    assert is_safe_url(url, allow_private_hosts=True) is True


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/x",
        "file:///etc/passwd",
        "not a url",
        "https://",
    ],
)
def test_allow_private_hosts_still_rejects_bad_schemes_and_hosts(url):
    assert is_safe_url(url, allow_private_hosts=True) is False


def test_allow_private_hosts_defaults_to_off():
    """Existing callers (web-search fetch, MCP index_url) keep the strict guard."""
    assert is_safe_url("http://169.254.169.254/latest/meta-data") is False


def _patch_resolver(monkeypatch: pytest.MonkeyPatch, *addresses: str, error: Exception | None = None) -> None:
    def _resolve(host, port, *_a, **_k):
        if error is not None:
            raise error
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (addr, port)) for addr in addresses]

    monkeypatch.setattr(socket, "getaddrinfo", _resolve)


@pytest.mark.asyncio
async def test_resolution_returns_the_addresses_to_pin_to(monkeypatch):
    _patch_resolver(monkeypatch, "93.184.216.34", "8.8.8.8")

    result = await resolve_public_addresses("https", "cozy.example.com", None)

    assert result.status == RESOLUTION_PUBLIC
    assert result.is_public
    # Resolver order preserved: getaddrinfo puts the preferred address first.
    assert result.addresses == ("93.184.216.34", "8.8.8.8")


@pytest.mark.asyncio
async def test_one_internal_record_blocks_and_returns_no_addresses(monkeypatch):
    _patch_resolver(monkeypatch, "93.184.216.34", "169.254.169.254")

    result = await resolve_public_addresses("https", "evil.example.com", None)

    assert result.status == RESOLUTION_BLOCKED
    assert result.addresses == ()


@pytest.mark.asyncio
async def test_a_name_with_no_records_is_unknown_not_a_resolver_failure(monkeypatch):
    _patch_resolver(monkeypatch, error=socket.gaierror(socket.EAI_NONAME, "Name or service not known"))

    result = await resolve_public_addresses("https", "nowhere.example.com", None)

    assert result.status == RESOLUTION_UNKNOWN_HOST


@pytest.mark.parametrize("errno", [socket.EAI_AGAIN, socket.EAI_FAIL])
@pytest.mark.asyncio
async def test_a_servfail_is_our_resolver_failing_not_a_bad_name(monkeypatch, errno):
    """The caller must not be told their URL is bad because our DNS hiccuped."""
    _patch_resolver(monkeypatch, error=socket.gaierror(errno, "Temporary failure in name resolution"))

    result = await resolve_public_addresses("https", "cozy.example.com", None)

    assert result.status == RESOLUTION_RESOLVER_FAILED
    assert not result.is_public
