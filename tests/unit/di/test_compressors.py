"""Compressor construction falls back to passthrough rather than failing boot."""

from __future__ import annotations

from types import SimpleNamespace

from core.compression import compressor_registry
from di.compressors import create_compressor, register_compressors


def _settings(**kwargs):
    defaults = {"enabled": True, "backend": "headroom", "target_ratio": None, "min_chars": 0, "timeout_s": 5.0}
    return SimpleNamespace(compression=SimpleNamespace(**{**defaults, **kwargs}, extra={}))


def test_registration_exposes_both_backends():
    register_compressors()
    assert {"noop", "headroom"} <= set(compressor_registry.list_registered())


def test_disabled_config_returns_noop():
    assert create_compressor(_settings(enabled=False)).name == "noop"


def test_unknown_backend_falls_back_to_noop():
    register_compressors()
    assert create_compressor(_settings(backend="does-not-exist")).name == "noop"


def test_unavailable_backend_falls_back_to_noop(monkeypatch):
    """headroom-ai is not a test dependency, so constructing it must degrade."""
    register_compressors()
    monkeypatch.setitem(__import__("sys").modules, "headroom", None)
    assert create_compressor(_settings(backend="headroom")).name == "noop"
