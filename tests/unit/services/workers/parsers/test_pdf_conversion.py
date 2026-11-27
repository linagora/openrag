"""The PDF loaders convert through the conversion they are given.

The loaders turn a converted PDF into pages and image blocks; the Ray adapters
call the pools exactly as the loaders did, and the Marker helpers an engine
outside Ray needs import without Ray.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from core.models.document import Document, DocumentType
from services.workers.parsers import docling_workers, marker_runtime, marker_workers
from services.workers.parsers.parser_dispatcher import ParserDispatcher

_PACKAGE_ROOT = Path(__file__).resolve().parents[5] / "openrag"


class _Conversion:
    def __init__(self, result=None, error: BaseException | None = None):
        self.result = result
        self.error = error
        self.paths: list[str] = []

    async def convert(self, file_path: str):
        self.paths.append(file_path)
        if self.error is not None:
            raise self.error
        return self.result


def _pdf() -> Document:
    return Document(filename="a.pdf", content_type=DocumentType.PDF, raw_bytes=b"%PDF-1.7")


# ── Loaders ──────────────────────────────────────────────────────────


async def test_the_marker_loader_builds_its_pages_from_its_conversion():
    conversion = _Conversion(result=("[PAGE_SEP]first{1}[PAGE_SEP]second{2}[PAGE_SEP]", {}))

    processed = await marker_workers.MarkerLoader(conversion).parse(_pdf())

    assert len(conversion.paths) == 1
    assert [(block.page_number, block.text) for block in processed.text_blocks] == [(1, "first"), (2, "second")]
    assert processed.page_count == 2


@pytest.mark.parametrize(("limit_mb", "named"), [(1418, True), (0, False)])
async def test_the_marker_loader_names_the_memory_setting_whatever_ran_out(monkeypatch, limit_mb, named):
    loader = marker_workers.MarkerLoader(_Conversion(error=MemoryError("out of memory")))
    loader.config = SimpleNamespace(loader=SimpleNamespace(marker_parse_memory_limit_mb=limit_mb))

    with pytest.raises(MemoryError) as raised:
        await loader._convert_pdf("/tmp/doc.pdf")

    assert str(raised.value).startswith("Marker ran out of memory")
    assert ("MARKER_PARSE_MEMORY_LIMIT_MB=1418 MiB" in str(raised.value)) is named
    assert isinstance(raised.value.__cause__, MemoryError)


async def test_the_docling_loader_converts_through_its_conversion():
    result = object()
    conversion = _Conversion(result=result)

    assert await docling_workers.DoclingLoader(conversion)._dispatch("/tmp/doc.pdf") is result
    assert conversion.paths == ["/tmp/doc.pdf"]


def test_the_dispatcher_builds_the_loaders_on_the_ray_pools():
    dispatcher = ParserDispatcher(
        SimpleNamespace(loader=SimpleNamespace(file_loaders=SimpleNamespace(pdf="MarkerLoader")))
    )

    marker = dispatcher._build_marker()._pool
    docling = dispatcher._build_docling()._pool

    assert isinstance(marker, marker_workers.MarkerLoader)
    assert isinstance(marker._conversion, marker_workers.RayMarkerConversion)
    assert isinstance(docling, docling_workers.DoclingLoader)
    assert isinstance(docling._conversion, docling_workers.RayDoclingConversion)


# ── Ray adapters ─────────────────────────────────────────────────────


def _recording_call(monkeypatch, module, outcome):
    calls: list[dict] = []

    async def call(future, timeout, task_description):
        calls.append({"future": future, "timeout": timeout, "task_description": task_description})
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(module, "call_ray_actor_with_timeout", call)
    return calls


def _adapter(cls, **loader_settings):
    adapter = cls.__new__(cls)
    adapter.config = SimpleNamespace(loader=SimpleNamespace(**loader_settings))
    adapter._pool = lambda: SimpleNamespace(process_pdf=SimpleNamespace(remote=lambda path: f"ref:{path}"))
    return adapter


async def test_the_marker_ray_conversion_calls_the_pool_as_the_loader_did(monkeypatch):
    calls = _recording_call(monkeypatch, marker_workers, ("markdown", {"k": "image"}))
    adapter = _adapter(marker_workers.RayMarkerConversion, marker_timeout=60)

    assert await adapter.convert("/tmp/doc.pdf") == ("markdown", {"k": "image"})
    assert calls == [
        {"future": "ref:/tmp/doc.pdf", "timeout": 60, "task_description": "MarkerLoader PDF loading (/tmp/doc.pdf)"}
    ]


async def test_the_marker_ray_conversion_raises_a_memory_error_found_in_the_ray_error(monkeypatch):
    ray_error = RuntimeError("MarkerLoader PDF loading (/tmp/doc.pdf) failed")
    ray_error.__cause__ = MemoryError("Marker parse of [p0-9] ran out of memory")
    _recording_call(monkeypatch, marker_workers, ray_error)
    adapter = _adapter(marker_workers.RayMarkerConversion, marker_timeout=60)

    with pytest.raises(MemoryError) as raised:
        await adapter.convert("/tmp/doc.pdf")

    assert raised.value.__cause__ is ray_error


async def test_the_marker_ray_conversion_lets_other_errors_through(monkeypatch):
    failure = RuntimeError("not a PDF")
    _recording_call(monkeypatch, marker_workers, failure)
    adapter = _adapter(marker_workers.RayMarkerConversion, marker_timeout=60)

    with pytest.raises(RuntimeError) as raised:
        await adapter.convert("/tmp/doc.pdf")

    assert raised.value is failure


async def test_the_docling_ray_conversion_calls_the_pool_as_the_loader_did(monkeypatch):
    result = object()
    calls = _recording_call(monkeypatch, docling_workers, result)
    adapter = _adapter(docling_workers.RayDoclingConversion, docling_timeout=90)

    assert await adapter.convert("/tmp/doc.pdf") is result
    assert calls == [
        {"future": "ref:/tmp/doc.pdf", "timeout": 90, "task_description": "DoclingLoader PDF loading (/tmp/doc.pdf)"}
    ]


# ── Marker helpers ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("total_pages", "chunk_size", "chunks"),
    [
        (
            25,
            10,
            [(list(range(10)), "[p0-9]"), (list(range(10, 20)), "[p10-19]"), ([20, 21, 22, 23, 24], "[p20-24]")],
        ),
        (8, 10, [(None, "(8p)")]),
        (8, 0, [(None, "(all pages)")]),
        (0, 10, [(None, "(0p)")]),
    ],
)
def test_a_pdf_is_converted_in_page_chunks(total_pages, chunk_size, chunks):
    assert marker_runtime.plan_chunks(total_pages, chunk_size) == chunks


@pytest.mark.parametrize(
    ("chunk_size", "chunks"),
    [
        (2, [([0, 1], "[p0-1]"), ([2], "[p2-2]")]),
        (5, [([0, 1, 2], "(3p)")]),
        (0, [([0, 1, 2], "(first 3p)")]),
    ],
)
def test_a_pdf_over_the_page_cap_is_converted_up_to_the_cap_only(monkeypatch, chunk_size, chunks):
    monkeypatch.setattr(marker_runtime, "_MAX_PDF_PAGES", 3)

    assert marker_runtime.plan_chunks(10, chunk_size) == chunks


def test_the_chunks_are_joined_in_page_order():
    assert marker_runtime.join_chunks([("first", {"a": 1}), ("second", {"b": 2})]) == (
        "first\n\nsecond",
        {"a": 1, "b": 2},
    )


@pytest.mark.parametrize(
    "module",
    [
        "services.workers.parsers.marker_runtime",
        "services.workers.parsers.pdf_conversion",
        "services.workers.parsers.docling_runtime",
        "services.workers.parsers.docling_engine",
        "core.utils.retry",
    ],
)
def test_an_engine_outside_ray_imports_its_helpers_without_ray(module):
    loaded = subprocess.run(
        [sys.executable, "-c", f"import sys, {module}; print('ray' in sys.modules)"],
        cwd=_PACKAGE_ROOT,
        env={**os.environ, "PYTHONPATH": str(_PACKAGE_ROOT)},
        capture_output=True,
        text=True,
        timeout=240,
        check=True,
    )

    assert loaded.stdout.strip().splitlines()[-1] == "False"
