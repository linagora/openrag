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
