"""Tests for deployment-provided Admin UI destinations."""

import pytest
from api.runtime_ui import get_grafana_url


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("", None),
        ("  ", None),
        (
            "https://grafana.example/d/openrag-http/openrag-http-metrics",
            "https://grafana.example/d/openrag-http/openrag-http-metrics",
        ),
        (
            " http://localhost:3000/d/openrag-http/openrag-http-metrics ",
            "http://localhost:3000/d/openrag-http/openrag-http-metrics",
        ),
        ("/grafana/d/openrag-http/openrag-http-metrics", "/grafana/d/openrag-http/openrag-http-metrics"),
        ("//untrusted.example/dashboard", None),
        ("javascript:alert(1)", None),
        ("grafana.example/dashboard", None),
    ],
)
def test_grafana_url_accepts_only_browser_safe_destinations(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("GRAFANA_URL", raising=False)
    else:
        monkeypatch.setenv("GRAFANA_URL", value)

    assert get_grafana_url() == expected
