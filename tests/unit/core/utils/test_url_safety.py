"""Tests for the SSRF URL guard."""

from __future__ import annotations

import socket

import pytest
from core.utils.url_safety import is_safe_url


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


def test_blocks_decimal_host_overflowing_into_a_public_ipv6_address():
    """ipaddress.ip_address(int(host)) used to accept any integer up to
    2**128-1, silently building an IPv6Address once the value exceeded
    2**32-1 instead of raising. A 38-digit decimal host crafted to land in a
    public IPv6 range therefore passed as "safe", while every real resolver
    (confirmed against socket.getaddrinfo) truncates it mod 2**32 down to
    127.0.0.1 underneath — an SSRF bypass into loopback."""
    host = "42535295865117307932921825931101732865"
    assert socket.inet_aton(host) == socket.inet_aton("127.0.0.1")
    assert is_safe_url(f"http://{host}/x") is False


def test_decimal_ipv4_form_still_blocked_without_the_removed_branch():
    """The dedicated ip_address(int(host)) branch is gone; inet_aton alone
    must still catch the ordinary in-range decimal spelling."""
    assert is_safe_url("http://2130706433/x") is False
