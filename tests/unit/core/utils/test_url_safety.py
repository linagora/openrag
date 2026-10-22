"""Tests for the SSRF URL guard."""

from __future__ import annotations

import pytest
from core.utils.url_safety import is_safe_url

_OVER_LIMIT_HOSTNAME = ".".join(["a" * 63, "b" * 63, "c" * 63, "d" * 62])


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
        "http://" + "a" * 64 + ".com/x",  # label past the 63-character DNS limit
    ],
)
def test_blocks_unsafe(url):
    assert is_safe_url(url) is False


def test_allows_hostnames_at_the_dns_length_limits():
    # 62 + 62 + 63 + 63 characters plus three dots = 253, every label in bounds.
    at_limit = ".".join(["a" * 62, "b" * 62, "c" * 63, "d" * 63])
    assert len(at_limit) == 253
    assert is_safe_url("http://" + at_limit + "/x") is True
    assert is_safe_url("http://" + at_limit + "./x") is True  # valid root dot
    assert is_safe_url("http://example.com./x") is True  # trailing dot is fine


@pytest.mark.parametrize("allow_private_hosts", [False, True])
def test_blocks_total_hostname_length_over_253_with_valid_labels(allow_private_hosts):
    # 63 + 63 + 63 + 62 characters plus three dots = 254; no label is too long.
    assert len(_OVER_LIMIT_HOSTNAME) == 254
    assert (
        is_safe_url(
            "http://" + _OVER_LIMIT_HOSTNAME + "/x",
            allow_private_hosts=allow_private_hosts,
        )
        is False
    )


@pytest.mark.parametrize("separator", ["。", "．", "｡"])
def test_allows_idna_equivalent_label_separators(separator):
    hostname = "a" * 32 + separator + "b" * 32
    assert is_safe_url(f"http://{hostname}/x") is True


def test_allow_private_hosts_permits_internal_hostname():
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
