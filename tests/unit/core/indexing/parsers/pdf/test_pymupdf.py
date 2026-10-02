"""Unit tests for the pymupdf ``DocumentParser`` (issue #640 fallback path)."""

from __future__ import annotations

import asyncio
import concurrent.futures
import multiprocessing
import os
import sys
from unittest.mock import patch

import pymupdf
import pytest
from core.indexing.parsers.pdf import pymupdf as pymupdf_mod
from core.indexing.parsers.pdf.pymupdf import PyMuPDFParser, _extract_markdown
from core.models.document import Document, DocumentType, ImageBlock

_TO_MARKDOWN = "core.indexing.parsers.pdf.pymupdf.pymupdf4llm.to_markdown"


def _minimal_pdf_bytes() -> bytes:
    doc = pymupdf.open()
    try:
        page = doc.new_page()
        page.insert_text((72, 72), "hello world")
        return doc.tobytes()
    finally:
        doc.close()


class TestExtractMarkdownFallback:
    def test_retries_once_against_cleaned_copy_on_runtime_error(self):
        raw = _minimal_pdf_bytes()
        good_chunks = [{"text": "hello world"}]

        with patch(_TO_MARKDOWN, side_effect=[RuntimeError("code=4: no font file for digest"), good_chunks]) as mock:
            pages, images = _extract_markdown(raw, "broken.pdf")

        assert pages == ["hello world"]
        assert images == []
        assert mock.call_count == 2

    def test_only_retries_once_and_propagates_if_still_failing(self):
        raw = _minimal_pdf_bytes()

        with patch(_TO_MARKDOWN, side_effect=RuntimeError("still broken")) as mock:
            with pytest.raises(RuntimeError, match="still broken"):
                _extract_markdown(raw, "broken.pdf")

        assert mock.call_count == 2

    def test_no_retry_when_first_attempt_succeeds(self):
        raw = _minimal_pdf_bytes()
        good_chunks = [{"text": "hello world"}]

        with patch(_TO_MARKDOWN, return_value=good_chunks) as mock:
            pages, _ = _extract_markdown(raw, "clean.pdf")

        assert pages == ["hello world"]
        assert mock.call_count == 1


class TestPyMuPDFParserRecovery:
    @pytest.mark.asyncio
    async def test_parse_recovers_from_transient_runtime_error(self):
        raw = _minimal_pdf_bytes()
        document = Document(filename="broken.pdf", content_type=DocumentType.PDF, raw_bytes=raw)
        good_chunks = [{"text": "hello world"}]

        with patch(_TO_MARKDOWN, side_effect=[RuntimeError("code=4: no font file for digest"), good_chunks]):
            result = await PyMuPDFParser().parse(document)

        assert [block.text for block in result.text_blocks] == ["hello world"]
        assert result.page_count == 1


class TestRecoveryPathResidency:
    def test_failed_document_is_closed_before_the_cleaned_copy_is_opened(self):
        """Peak memory on the #640 recovery path (#846).

        The failed document and the cleaned copy used to be open at the same
        time, so MuPDF held parsed structures for both. Closing the first before
        opening the second releases one of them. ``raw`` itself belongs to the
        caller's Document and stays live either way — this is the part the
        parser can actually control.
        """
        raw = _minimal_pdf_bytes()
        opened: list[pymupdf.Document] = []
        real_open = pymupdf.open
        closed_when_second_opened: list[bool] = []

        def tracking_open(*args, **kwargs):
            if opened:
                closed_when_second_opened.append(opened[0].is_closed)
            doc = real_open(*args, **kwargs)
            opened.append(doc)
            return doc

        with (
            patch("core.indexing.parsers.pdf.pymupdf.pymupdf.open", side_effect=tracking_open),
            patch(_TO_MARKDOWN, side_effect=[RuntimeError("code=4"), [{"text": "ok"}]]),
        ):
            pages, _ = _extract_markdown(raw, "broken.pdf")

        assert pages == ["ok"]
        assert closed_when_second_opened == [True], "the failed document was still open"


# ---------------------------------------------------------------------------
# The parse pool — child processes, not a shared thread (#997, audit A2)
# ---------------------------------------------------------------------------


class TestParsesRunInChildProcesses:
    """PyMuPDF is not thread-safe, so parses used to serialize onto one shared
    thread. Each process has its own MuPDF state, so they run in a pool instead:
    that lifts the serialization *and* gives the parse a memory ceiling."""

    @pytest.fixture(autouse=True)
    def _configured_pool(self):
        """These pin the *configured* pool; the default is threads (see below)."""
        pymupdf_mod.configure_pool(pymupdf_mod.PyMuPDFPoolSettings(max_workers=2))
        yield
        pymupdf_mod.configure_pool(pymupdf_mod.PyMuPDFPoolSettings())

    def test_the_default_is_still_one_thread_and_spawns_nothing(self):
        """Unconfigured must mean unchanged. This parser is also built in every
        API replica for the direct-extract path, where a first parse would spawn
        on the loop that serves /health_check — and under `-m api.main` a spawned
        child re-imports the whole app module. Opt-in, not a side effect."""
        pymupdf_mod.configure_pool(pymupdf_mod.PyMuPDFPoolSettings())
        assert isinstance(pymupdf_mod._get_pool(), concurrent.futures.ThreadPoolExecutor)

    @pytest.mark.parametrize(
        "settings",
        [
            pymupdf_mod.PyMuPDFPoolSettings(max_workers=2),
            pymupdf_mod.PyMuPDFPoolSettings(memory_limit_mb=4096),
        ],
        ids=["parallelism asked for", "a ceiling asked for"],
    )
    def test_asking_for_either_capability_gets_processes(self, settings):
        pymupdf_mod.configure_pool(settings)
        assert isinstance(pymupdf_mod._get_pool(), concurrent.futures.ProcessPoolExecutor)

    @pytest.mark.asyncio
    async def test_the_parse_really_happens_in_another_process(self):
        """Guard for the whole design: if this ever runs in-process again, the
        ceiling stops being enforceable and the thread-safety issue is back."""
        document = Document(filename="report.pdf", content_type=DocumentType.PDF, raw_bytes=_minimal_pdf_bytes())
        result = await PyMuPDFParser().parse(document)

        assert [block.text for block in result.text_blocks] == ["hello world"]
        assert await asyncio.get_running_loop().run_in_executor(pymupdf_mod._get_pool(), os.getpid) != os.getpid()

    @pytest.mark.asyncio
    async def test_extracted_images_survive_the_process_boundary(self):
        """``ImageBlock.image_bytes`` is declared ``exclude=True``. That governs
        ``model_dump``, not ``pickle`` — but if it ever governed both, every
        image would silently arrive empty after crossing back."""
        block = ImageBlock(image_bytes=b"PNG-PAYLOAD" * 64, page_number=2, metadata={"markdown_ref": "![](x-1)"})
        loop = asyncio.get_running_loop()
        returned = await loop.run_in_executor(pymupdf_mod._get_pool(), _echo, block)

        assert returned.image_bytes == block.image_bytes
        assert returned.metadata == block.metadata and returned.page_number == 2

    def test_a_broken_pool_is_discarded_so_later_parses_recover(self):
        """A child killed outright (OOM killer, MuPDF segfault) breaks the whole
        executor; without discarding it every later parse in this worker fails."""
        pool = pymupdf_mod._get_pool()
        pymupdf_mod._discard_pool(pool)

        assert pymupdf_mod._get_pool() is not pool, "the broken pool was reused"

    def test_reconfiguring_replaces_a_pool_built_on_the_old_settings(self):
        pymupdf_mod.configure_pool(pymupdf_mod.PyMuPDFPoolSettings(max_workers=1))
        first = pymupdf_mod._get_pool()

        pymupdf_mod.configure_pool(pymupdf_mod.PyMuPDFPoolSettings(max_workers=2))
        assert pymupdf_mod._get_pool() is not first

        again = pymupdf_mod._get_pool()
        pymupdf_mod.configure_pool(pymupdf_mod.PyMuPDFPoolSettings(max_workers=2))
        assert pymupdf_mod._get_pool() is again, "identical settings rebuilt the pool for nothing"

    @pytest.mark.skipif(not sys.platform.startswith("linux"), reason="RLIMIT_DATA only covers mmap on Linux")
    def test_the_ceiling_does_not_refuse_file_backed_mappings(self):
        """Pins ``RLIMIT_DATA`` over ``RLIMIT_AS``. The two are indistinguishable
        for a runaway allocation — both refuse it — so only a file-backed mapping
        separates them, and ``RLIMIT_AS`` refusing one is a PDF failing to open."""
        ctx = multiprocessing.get_context("fork")
        with concurrent.futures.ProcessPoolExecutor(max_workers=1, mp_context=ctx) as pool:
            assert pool.submit(_bounded_mmap, 256).result(timeout=60) == "mapped"

    @pytest.mark.skipif(not sys.platform.startswith("linux"), reason="RLIMIT_DATA only covers mmap on Linux")
    def test_the_memory_ceiling_bounds_a_parse(self):
        """Run it for real: asserting ``setrlimit`` was called would pass just as
        well with ``RLIMIT_AS``, which would refuse the mappings a parse needs."""
        ctx = multiprocessing.get_context("fork")
        with concurrent.futures.ProcessPoolExecutor(max_workers=1, mp_context=ctx) as pool:
            assert pool.submit(_bounded_alloc, 256, 1024).result(timeout=60) == "MemoryError"
            assert pool.submit(_bounded_alloc, 256, 32).result(timeout=60) == "allocated"


def _echo(value):
    return value


def _bounded_alloc(headroom_mb: int, alloc_mib: int) -> str:
    """Apply a ceiling relative to this process, then try to blow through it."""
    from core.indexing.parsers.pdf.pymupdf import _pool_worker_init

    with open("/proc/self/status") as status:
        vmdata = next(int(line.split()[1]) // 1024 for line in status if line.startswith("VmData:"))
    _pool_worker_init(vmdata + headroom_mb)
    try:
        buf = bytearray(alloc_mib * 1024 * 1024)
        return "allocated" if buf else "empty"
    except MemoryError:
        return "MemoryError"


def _bounded_mmap(headroom_mb: int) -> str:
    import mmap
    import tempfile

    from core.indexing.parsers.pdf.pymupdf import _pool_worker_init

    with open("/proc/self/status") as status:
        vmdata = next(int(line.split()[1]) // 1024 for line in status if line.startswith("VmData:"))
    _pool_worker_init(vmdata + headroom_mb)
    with tempfile.NamedTemporaryFile() as fh:
        fh.truncate(2 * 1024 * 1024 * 1024)
        try:
            with mmap.mmap(fh.fileno(), 0, prot=mmap.PROT_READ):
                return "mapped"
        except OSError as exc:
            return f"refused: {exc.errno}"
