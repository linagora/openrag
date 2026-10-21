import pytest
from core.utils.web_url import normalize_web_url


@pytest.mark.parametrize(
    "value",
    [
        None,
        42,
        "",
        "javascript:alert(1)",
        "https://",
        "http://[::1",
    ],
)
def test_normalize_web_url_rejects_unrenderable_values(value):
    assert normalize_web_url(value) is None


def test_normalize_web_url_returns_canonical_http_url():
    assert normalize_web_url("  https://example.com/a path  ") == "https://example.com/a%20path"
