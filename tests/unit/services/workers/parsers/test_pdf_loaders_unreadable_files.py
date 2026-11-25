"""Marker and Docling report a PDF they cannot open as the file's fault (#1490).

Before, the pool's ``PdfiumError`` reached the task as the loader's Ray wrapper,
``RuntimeError: MarkerLoader PDF loading (/app/data/...) failed: PdfiumError: ...``:
untyped, so a client could not tell it from an outage, and naming where the
upload is stored.
"""

from __future__ import annotations

import asyncio
import pickle
import threading
from types import SimpleNamespace

import pypdfium2
import pytest
from core.indexing.pdf_integrity import CORRUPT_PDF_MESSAGE
from core.utils.exceptions import InvalidDocumentError
from ray.exceptions import RayTaskError
from services.inference.parsers._base_openai_parser import BaseOpenAIPdfClient
from services.workers.parsers import docling_workers, marker_workers

STORED_IN = "/srv/openrag/data/tenant-a"
STORED_AS = "a1b2c3_report.pdf"


def _marker_pool():
    pool_class = marker_workers.MarkerPool.__ray_metadata__.modified_class
    pool = pool_class.__new__(pool_class)
    pool.logger = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)
    pool.config = SimpleNamespace(
        loader=SimpleNamespace(marker_chunk_size=10, marker_max_task_retry=3, marker_retry_base_delay=0.01)
    )
    return pool


def _docling_pool():
    pool_class = docling_workers.DoclingPool.__ray_metadata__.modified_class
    pool = pool_class.__new__(pool_class)
    pool.logger = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)
    pool.config = SimpleNamespace(
        loader=SimpleNamespace(docling_timeout=5, docling_max_task_retry=3, docling_retry_base_delay=0.01)
    )
    return pool


@pytest.mark.parametrize("make_pool", [_marker_pool, _docling_pool], ids=["marker", "docling"])
async def test_the_pool_refuses_an_unreadable_pdf_before_taking_a_worker(tmp_path, pdf_samples, make_pool):
    """Checked once, before a slot is taken and outside the retries: the same
    bytes fail the same way on every attempt."""
    path = tmp_path / STORED_AS
    path.write_bytes(pdf_samples.truncated)
    pool = make_pool()
    pool._queue = asyncio.Queue()
    pool._queue.put_nowait("worker")

    with pytest.raises(InvalidDocumentError) as raised:
        await pool.process_pdf(str(path))

    assert raised.value.code == "INVALID_PDF"
    assert pool._queue.qsize() == 1


@pytest.mark.parametrize(
    ("make_pool", "module"),
    [(_marker_pool, marker_workers), (_docling_pool, docling_workers)],
    ids=["marker", "docling"],
)
async def test_the_pool_opens_the_pdf_off_its_event_loop(monkeypatch, make_pool, module):
    """pdfium scans a damaged file whole (about 6 ms per MB): on the loop, that
    would stall every other conversion the pool is running."""
    loop_thread = threading.get_ident()
    opened_on = []

    def count_pages(_path):
        opened_on.append(threading.get_ident())
        raise InvalidDocumentError(CORRUPT_PDF_MESSAGE, code="INVALID_PDF")

    monkeypatch.setattr(module, "pdfium_page_count", count_pages)
    pool = make_pool()
    pool._queue = asyncio.Queue()

    with pytest.raises(InvalidDocumentError):
        await pool.process_pdf(f"{STORED_IN}/{STORED_AS}")

    assert opened_on and opened_on[0] != loop_thread


async def test_docling_converts_a_pdf_its_check_failed_on_for_another_reason(monkeypatch):
    """A failure that is not the file's fault is left to the conversion, which
    retries it, as it did before the check existed."""

    def unreadable(_path):
        raise pypdfium2.PdfiumError("Failed to load document (PDFium: File access error).")

    async def converted():
        return "conversion"

    monkeypatch.setattr(docling_workers, "pdfium_page_count", unreadable)
    pool = _docling_pool()
    pool._queue = asyncio.Queue()
    pool._queue.put_nowait(SimpleNamespace(convert=SimpleNamespace(remote=lambda _path: converted())))

    assert await pool.process_pdf(f"{STORED_IN}/{STORED_AS}") == "conversion"


def _pool_failing_with(cause: BaseException):
    """A pool handle whose ``process_pdf`` fails the way a Ray actor's does: the
    exception pickled across, then raised as ``RayTaskError(<its type>)``."""
    crossed = pickle.loads(pickle.dumps(cause))
    ray_error = RayTaskError("process_pdf", "Traceback ...", crossed).as_instanceof_cause()

    async def fail():
        raise ray_error

    return SimpleNamespace(process_pdf=SimpleNamespace(remote=lambda _path: fail()))


_LOADERS = pytest.mark.parametrize(
    ("loader_class", "dispatch", "description"),
    [
        (marker_workers.MarkerLoader, lambda loader, path: loader._convert_pdf(path), "MarkerLoader PDF loading"),
        (docling_workers.DoclingLoader, lambda loader, path: loader._dispatch(path), "DoclingLoader PDF loading"),
    ],
    ids=["marker", "docling"],
)


@_LOADERS
async def test_the_loader_raises_the_pool_s_error_not_ray_s_wrapper(monkeypatch, loader_class, dispatch, description):
    loader = loader_class()
    pool = _pool_failing_with(InvalidDocumentError(CORRUPT_PDF_MESSAGE, code="INVALID_PDF"))
    monkeypatch.setattr(loader, "_pool", lambda: pool)

    with pytest.raises(InvalidDocumentError) as raised:
        await dispatch(loader, f"{STORED_IN}/{STORED_AS}")

    error = raised.value
    assert type(error) is InvalidDocumentError
    assert (error.code, error.message, error.status_code) == ("INVALID_PDF", CORRUPT_PDF_MESSAGE, 422)
    # What it was raised from names the file, never where it is stored.
    assert str(error.__cause__) == f"{description} ({STORED_AS}) failed: InvalidDocumentError: {error}"


@_LOADERS
async def test_any_other_failure_names_the_file_not_where_it_is_stored(
    monkeypatch, loader_class, dispatch, description
):
    loader = loader_class()
    pool = _pool_failing_with(ValueError("bad xref table"))
    monkeypatch.setattr(loader, "_pool", lambda: pool)

    with pytest.raises(RuntimeError) as raised:
        await dispatch(loader, f"{STORED_IN}/{STORED_AS}")

    assert str(raised.value) == f"{description} ({STORED_AS}) failed: ValueError: bad xref table"


def test_dotsocr_refuses_an_unreadable_pdf(pdf_samples):
    with pytest.raises(InvalidDocumentError) as raised:
        BaseOpenAIPdfClient._render_pdf_pages(pdf_samples.truncated, scale=1.0)

    assert raised.value.code == "INVALID_PDF"
