from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest
from core.models.document import Document, DocumentType
from services.workers.parsers import docling_workers


def _config(docling_num_gpus: float = 0.25):
    """Build the minimal config shape consumed by Docling GPU selection."""
    return SimpleNamespace(loader=SimpleNamespace(docling_num_gpus=docling_num_gpus))


def test_docling_num_gpus_uses_ray_cluster_resources_when_cuda_is_hidden(monkeypatch):
    """Docling must request a GPU from Ray even when local CUDA is hidden in the
    pool process (CUDA_VISIBLE_DEVICES=""), otherwise it silently parses on CPU."""
    monkeypatch.setattr(docling_workers.torch.cuda, "is_available", lambda: False)
    monkeypatch.setattr(docling_workers.ray, "cluster_resources", lambda: {"GPU": 1.0})

    assert docling_workers._docling_num_gpus(_config()) == 0.25


def test_docling_num_gpus_zero_when_cluster_has_no_gpu(monkeypatch):
    monkeypatch.setattr(docling_workers.torch.cuda, "is_available", lambda: False)
    monkeypatch.setattr(docling_workers.ray, "cluster_resources", dict)

    assert docling_workers._docling_num_gpus(_config()) == 0


def test_docling_num_gpus_zero_when_not_requested(monkeypatch):
    monkeypatch.setattr(docling_workers.ray, "cluster_resources", lambda: {"GPU": 1.0})

    assert docling_workers._docling_num_gpus(_config(docling_num_gpus=0)) == 0


# --- #722: the detached pool supervisor must survive its own crashes ----------


def test_docling_pool_sets_max_restarts():
    """DoclingPool had a bare @ray.remote — Ray's default max_restarts is 0, so
    one crash left the detached actor permanently dead and every docling parse
    afterwards failed. It must be restartable like MarkerPool/WhisperActor."""
    assert docling_workers.DoclingPool.__ray_metadata__.max_restarts == 5


def test_docling_pool_restart_policy_matches_marker():
    """Parity guard: if MarkerPool's policy changes, this flags that DoclingPool
    should be reconsidered too, rather than silently drifting apart."""
    from services.workers.parsers.marker_workers import MarkerPool

    assert docling_workers.DoclingPool.__ray_metadata__.max_restarts == MarkerPool.__ray_metadata__.max_restarts


# ---------------------------------------------------------------------------
# DoclingLoader.parse keeps the event loop free while building the blocks
# ---------------------------------------------------------------------------


def _fake_conversion(n_pages: int, n_pictures: int, export=lambda page_no: "text"):
    """The slice of a ``ConversionResult`` that the block builders read."""
    picture = SimpleNamespace(image=SimpleNamespace(pil_image=object()))
    return SimpleNamespace(
        pages=[None] * n_pages,
        document=SimpleNamespace(export_to_markdown=export, pictures=[picture] * n_pictures),
    )


def _pdf() -> Document:
    return Document(id="d", filename="f.pdf", content_type=DocumentType.PDF, raw_bytes=b"%PDF")


async def test_parse_builds_blocks_off_the_event_loop(monkeypatch):
    """Exporting the pages of a large PDF must not starve other coroutines
    (worker lease renewals) running on the same loop."""
    loader = docling_workers.DoclingLoader.__new__(docling_workers.DoclingLoader)

    ticks = 0
    # Ticks seen when the export starts and ends: only that span counts, not the
    # temporary-file write around the dispatch.
    window: list[int] = []

    def _slow_export(page_no):
        window.append(ticks)
        time.sleep(0.5)
        window.append(ticks)
        return "text"

    async def _fake_dispatch(path):
        return _fake_conversion(n_pages=1, n_pictures=0, export=_slow_export)

    monkeypatch.setattr(loader, "_dispatch", _fake_dispatch)

    async def _ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    task = asyncio.create_task(_ticker())
    try:
        parsed = await loader.parse(_pdf())
    finally:
        task.cancel()

    during_export = window[1] - window[0]
    assert during_export >= 20, f"event loop was blocked while exporting the pages (ticks={during_export})"
    assert [block.text for block in parsed.text_blocks] == ["text"]


@pytest.mark.parametrize("slow_step", ["page export", "picture encoding"])
async def test_cancelling_parse_stops_building_the_blocks(monkeypatch, slow_step):
    """A cancelled parse must not leave the worker thread exporting every
    remaining page or encoding every remaining picture."""
    loader = docling_workers.DoclingLoader.__new__(docling_workers.DoclingLoader)
    total = 50
    done = 0
    started = threading.Event()

    def _slow(value):
        def _step(*_args, **_kwargs):
            nonlocal done
            done += 1
            started.set()
            time.sleep(0.02)
            return value

        return _step

    if slow_step == "page export":
        result = _fake_conversion(n_pages=total, n_pictures=0, export=_slow("text"))
    else:
        result = _fake_conversion(n_pages=1, n_pictures=total)
        monkeypatch.setattr(docling_workers, "pil_to_png_bytes", _slow(b"png"))

    async def _fake_dispatch(path):
        return result

    monkeypatch.setattr(loader, "_dispatch", _fake_dispatch)

    task = asyncio.create_task(loader.parse(_pdf()))
    deadline = time.monotonic() + 5
    while not started.is_set():
        if task.done():
            await task  # surfaces the parse error, if any
            pytest.fail(f"parse returned before its {slow_step} started")
        assert time.monotonic() < deadline, f"{slow_step} never started"
        await asyncio.sleep(0.005)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await asyncio.sleep(0.1)  # let the thread reach its next check
    settled = done
    await asyncio.sleep(0.3)
    assert done == settled < total
