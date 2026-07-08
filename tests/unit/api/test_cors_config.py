"""Tests for CORS origin sanitisation."""

from api.cors_config import sanitize_cors_origins


def test_drops_wildcard_when_credentialed():
    origins = ["https://app.example.com", "*"]
    assert sanitize_cors_origins(origins, allow_credentials=True) == ["https://app.example.com"]


def test_keeps_wildcard_when_no_credentials():
    origins = ["*"]
    assert sanitize_cors_origins(origins, allow_credentials=False) == ["*"]


def test_noop_without_wildcard():
    origins = ["https://a.example.com", "https://b.example.com"]
    assert sanitize_cors_origins(origins, allow_credentials=True) == origins


def test_drops_every_wildcard_occurrence():
    origins = ["*", "https://a.example.com", "*"]
    assert sanitize_cors_origins(origins, allow_credentials=True) == ["https://a.example.com"]
