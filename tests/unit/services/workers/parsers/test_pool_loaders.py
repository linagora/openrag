"""The pooled loaders must create their Ray pool lazily via
``get_or_create_actor`` rather than assuming bootstrap already created it.

Bootstrap no longer pre-warms any parser pool, and a per-preset
``parsing_strategy`` can select any backend at runtime (#569). A get-only
``ray.get_actor`` then fails with "Failed to look up actor 'DoclingPool'"
(#575); lazy creation makes whichever backend is picked work on first use.
"""

from __future__ import annotations

import importlib
import weakref

import pytest
from core.models.document import Document, DocumentType

_POOL_LOADERS = pytest.mark.parametrize(
    ("module_name", "loader_name", "pool_name", "pool_attr"),
    [
        ("services.workers.parsers.docling_workers", "DoclingLoader", "DoclingPool", "DoclingPool"),
        ("services.workers.parsers.marker_workers", "MarkerLoader", "MarkerPool", "MarkerPool"),
        ("services.workers.parsers.whisper_workers", "LocalWhisperLoader", "WhisperPool", "WhisperPool"),
    ],
)

# How each loader hands one file to its pool.
_DISPATCH = {
    "DoclingLoader": lambda loader: loader._dispatch("/tmp/doc.pdf"),
    "MarkerLoader": lambda loader: loader._convert_pdf("/tmp/doc.pdf"),
    "LocalWhisperLoader": lambda loader: loader.parse(
        Document(filename="a.wav", content_type=DocumentType.AUDIO, raw_bytes=b"wav")
    ),
}


class _ActorHandle:
    """Behaves like a Ray ``ActorHandle`` where it matters here: the
    ``ActorMethod`` it hands out holds the handle weakly, so calling ``.remote()``
    off a handle nobody stored raises "Lost reference to actor"."""

    def __init__(self, name: str):
        self.name = name

    def __getattr__(self, method_name: str):
        return _ActorMethod(self)


class _ActorMethod:
    def __init__(self, handle: _ActorHandle):
        self._handle = weakref.ref(handle)

    def remote(self, *args):
        handle = self._handle()
        if handle is None:
            raise RuntimeError("Lost reference to actor.")

        async def result():
            return handle.name

        return result()


@_POOL_LOADERS
def test_pool_loader_lazily_creates_its_pool(monkeypatch, module_name, loader_name, pool_name, pool_attr):
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

    assert loader._pool() == "pool-handle"
    assert calls == [(pool_name, PoolCls, {"lifetime": "detached"})]


@_POOL_LOADERS
async def test_pool_loader_follows_a_restarted_pool(monkeypatch, module_name, loader_name, pool_name, pool_attr):
    """The loader lives as long as its indexer worker, but
    ``POST /actors/{pool}/restart`` replaces the named pool with a new actor. A
    handle cached at construction kept dispatching to the killed pool, so every
    later parse failed with ``ActorDiedError`` until the stack was restarted.

    Dispatches through the real call site with Ray's weak ``ActorMethod``
    semantics: looking the pool up per call must not leave the handle unstored
    (``self._pool().process_pdf.remote()`` raised "Lost reference to actor" on
    every file)."""
    import services.workers.bootstrap as bootstrap

    module = importlib.import_module(module_name)
    # Build each handle inside the lookup, as Ray does: handles built up front stay
    # referenced by their list, which would hide the weak-reference failure.
    names = iter(("pool-before-restart", "pool-after-restart"))
    monkeypatch.setattr(bootstrap, "get_or_create_actor", lambda name, cls, **options: _ActorHandle(next(names)))

    async def await_ref(ref, **_kwargs):
        return await ref

    if hasattr(module, "call_ray_actor_with_timeout"):
        monkeypatch.setattr(module, "call_ray_actor_with_timeout", await_ref)

    loader = getattr(module, loader_name)()
    dispatch = _DISPATCH[loader_name]

    for expected in ("pool-before-restart", "pool-after-restart"):
        result = await dispatch(loader)
        if loader_name == "LocalWhisperLoader":
            result = result.text_blocks[0].text
        assert result == expected
