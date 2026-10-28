"""Tests for the SSRF URL guard."""

from __future__ import annotations

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
        "http://127。0。0。1/x",  # IDNA-equivalent separators around loopback
        "http://127．0．0．1/x",
        "http://127｡1/x",
        "http://localhost./x",  # DNS root dot does not change localhost
        "http://0xa9fea9fe/x",  # hex-encoded 169.254.169.254
        "ftp://example.com/x",  # non-http scheme
        "file:///etc/passwd",
        "not a url",
        "http://" + "a" * 64 + ".com/x",  # label past the 63-character DNS limit
        "http://" + "a" * 300 + ".com/x",  # hostname past the 253-character DNS limit
    ],
)
def test_blocks_unsafe(url):
    assert is_safe_url(url) is False


def test_allows_hostnames_at_the_dns_length_limits():
    # 62 + 62 + 63 + 63 characters plus three dots = 253, every label in bounds.
    at_limit = ".".join(["a" * 62, "b" * 62, "c" * 63, "d" * 63])
    assert len(at_limit) == 253
    assert is_safe_url("http://" + at_limit + "/x") is True
    assert is_safe_url("http://" + at_limit + "e/x") is False  # 254 total
    assert is_safe_url("http://example.com./x") is True  # trailing dot is fine


def test_rejects_more_than_one_trailing_root_dot_at_the_length_limit():
    at_limit = ".".join(["a" * 62, "b" * 62, "c" * 63, "d" * 63])
    assert len(at_limit) == 253
    assert is_safe_url("http://" + at_limit + "../x") is False


@pytest.mark.parametrize("separator", ["。", "．", "｡"])
def test_idna_equivalent_separators_are_normalized_for_hostname_checks(separator: str) -> None:
    assert is_safe_url(f"https://one{separator}two.example/x") is True


def test_dns_length_limits_apply_in_private_host_mode():
    # allow_private_hosts skips the address checks, not the length bounds:
    # the fetch still resolves the hostname, so the idna CPU bound stays.
    assert is_safe_url("http://" + "a" * 300 + ".com/x", allow_private_hosts=True) is False
    assert is_safe_url("http://intranet.corp/x", allow_private_hosts=True) is True


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
    public IPv6 range therefore passed as "safe".

    inet_aton's own overflow handling is not portable enough to rely on here:
    glibc rejects a host this large outright (confirmed against
    socket.getaddrinfo — it fails to resolve, so there is no real bypass on
    Linux), but at least one other libc (BSD/macOS) silently wraps it mod
    2**32 into 127.0.0.1 instead of raising, which the guard must catch
    regardless of the platform it runs on. Hence the dedicated
    _bare_numeric_host_value overflow check ahead of inet_aton."""
    host = "42535295865117307932921825931101732865"
    assert int(host) > 0xFFFFFFFF
    assert is_safe_url(f"http://{host}/x") is False


def test_blocks_hex_host_overflowing_past_32_bits():
    """Same overflow class as the decimal case above, hex-spelled."""
    assert is_safe_url("http://0xfffffffff/x") is False


def test_decimal_ipv4_form_still_blocked_without_the_removed_branch():
    """The dedicated ip_address(int(host)) branch is gone; inet_aton alone
    must still catch the ordinary in-range decimal spelling."""
    assert is_safe_url("http://2130706433/x") is False
