"""The pooled loaders must create their Ray pool lazily via
``get_or_create_actor`` rather than assuming bootstrap already created it.

Bootstrap no longer pre-warms any parser pool, and a per-preset
``parsing_strategy`` can select any backend at runtime (#569). A get-only
``ray.get_actor`` then fails with "Failed to look up actor 'DoclingPool'"
(#575); lazy creation makes whichever backend is picked work on first use.
"""

from __future__ import annotations

import pytest


@pytest.mark.parametrize(
    ("module_name", "loader_name", "pool_name", "pool_attr"),
    [
        ("services.workers.parsers.docling_workers", "DoclingLoader", "DoclingPool", "DoclingPool"),
        ("services.workers.parsers.marker_workers", "MarkerLoader", "MarkerPool", "MarkerPool"),
        ("services.workers.parsers.whisper_workers", "LocalWhisperLoader", "WhisperPool", "WhisperPool"),
    ],
)
def test_pool_loader_lazily_creates_its_pool(monkeypatch, module_name, loader_name, pool_name, pool_attr):
    import importlib

    import services.workers.bootstrap as bootstrap

    module = importlib.import_module(module_name)
    Loader = getattr(module, loader_name)
    PoolCls = getattr(module, pool_attr)

    calls: list[tuple] = []

    def fake_get_or_create_actor(name, cls, **options):
        calls.append((name, cls, options))
        return "pool-handle"

    monkeypatch.setattr(bootstrap, "get_or_create_actor", fake_get_or_create_actor)
    # A get-only ray.get_actor must NOT be used anymore — fail loudly if it is.
    monkeypatch.setattr(module.ray, "get_actor", lambda *a, **k: pytest.fail("loader used get-only ray.get_actor"))

    loader = Loader()

    assert loader.worker == "pool-handle"
    assert calls == [(pool_name, PoolCls, {"lifetime": "detached"})]
