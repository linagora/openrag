"""Unit tests for the pymupdf ``DocumentParser`` (issue #640 and #1102 fallback paths)."""

from __future__ import annotations

import asyncio
import concurrent.futures
import ctypes
import functools
import multiprocessing
import os
import platform
import resource
import signal
import sys
import threading
import time
from unittest.mock import patch

import pymupdf
import pytest
from core.indexing.parsers.pdf import pymupdf as pymupdf_mod
from core.indexing.parsers.pdf.pymupdf import (
    _MARKDOWN_TEXT_RATIO_THRESHOLD,
    PyMuPDFParser,
    _extract_markdown,
    _is_allocation_failure,
    _pages_with_fallback,
)
from core.models.document import Document, DocumentType, ImageBlock
from core.utils import process_limits

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
            patch(_TO_MARKDOWN, side_effect=[RuntimeError("code=4"), [{"text": "hello world"}]]),
        ):
            pages, _ = _extract_markdown(raw, "broken.pdf")

        assert pages == ["hello world"]
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
        _drop_pool()

    def test_the_default_is_still_one_thread_and_spawns_nothing(self):
        """Unconfigured, parses stay on one dedicated thread, as before."""
        pymupdf_mod.configure_pool(pymupdf_mod.PyMuPDFPoolSettings())
        assert pymupdf_mod._get_pool().uses_processes is False

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
        assert pymupdf_mod._get_pool().uses_processes is True

    @pytest.mark.asyncio
    async def test_the_parse_really_happens_in_another_process(self):
        """Guard for the whole design: if this ever runs in-process again, the
        ceiling stops being enforceable and the thread-safety issue is back."""
        document = Document(filename="report.pdf", content_type=DocumentType.PDF, raw_bytes=_minimal_pdf_bytes())
        result = await PyMuPDFParser().parse(document)

        assert [block.text for block in result.text_blocks] == ["hello world"]
        assert await pymupdf_mod._get_pool().run(os.getpid) != os.getpid()

    @pytest.mark.asyncio
    async def test_a_child_opens_the_file_instead_of_receiving_its_bytes(self, tmp_path):
        """Bytes are pickled through a pipe: a transient copy in the parent per
        parse in flight, and the whole file in the child before it parses.
        ``raw_bytes`` here is not a PDF, so only opening the path can succeed."""
        path = tmp_path / "report.pdf"
        path.write_bytes(_minimal_pdf_bytes())
        document = Document(
            filename="report.pdf", content_type=DocumentType.PDF, raw_bytes=b"not a pdf", source_path=str(path)
        )

        result = await PyMuPDFParser().parse(document)
        assert [block.text for block in result.text_blocks] == ["hello world"]

    @pytest.mark.asyncio
    async def test_without_the_file_the_child_gets_the_bytes(self, tmp_path):
        document = Document(
            filename="report.pdf",
            content_type=DocumentType.PDF,
            raw_bytes=_minimal_pdf_bytes(),
            source_path=str(tmp_path / "gone.pdf"),
        )

        result = await PyMuPDFParser().parse(document)
        assert [block.text for block in result.text_blocks] == ["hello world"]

    @pytest.mark.asyncio
    async def test_on_the_thread_the_bytes_are_parsed(self, tmp_path):
        """No pipe to cross, so nothing to save: the thread parses what it was given."""
        pymupdf_mod.configure_pool(pymupdf_mod.PyMuPDFPoolSettings())
        path = tmp_path / "report.pdf"
        path.write_bytes(_minimal_pdf_bytes())
        document = Document(
            filename="report.pdf", content_type=DocumentType.PDF, raw_bytes=b"not a pdf", source_path=str(path)
        )

        with pytest.raises(pymupdf.FileDataError):
            await PyMuPDFParser().parse(document)

    @pytest.mark.asyncio
    async def test_extracted_images_survive_the_process_boundary(self):
        """``ImageBlock.image_bytes`` is declared ``exclude=True``. That governs
        ``model_dump``, not ``pickle`` — but if it ever governed both, every
        image would silently arrive empty after crossing back."""
        block = ImageBlock(image_bytes=b"PNG-PAYLOAD" * 64, page_number=2, metadata={"markdown_ref": "![](x-1)"})
        returned = await pymupdf_mod._get_pool().run(_echo, block)

        assert returned.image_bytes == block.image_bytes
        assert returned.metadata == block.metadata and returned.page_number == 2

    @pytest.mark.asyncio
    async def test_a_dead_child_is_replaced_so_later_parses_recover(self):
        """A child killed outright (OOM killer, MuPDF segfault) breaks its
        executor; without replacing it every later parse on that slot fails."""
        pool = pymupdf_mod._get_pool()
        for _ in range(2):  # whichever slot it lands on, both get exercised
            with pytest.raises(concurrent.futures.process.BrokenProcessPool):
                await pool.run(os._exit, 9)

        assert await pool.run(os.getpid) != os.getpid()

    @pytest.mark.asyncio
    async def test_a_child_dying_fails_only_its_own_parse(self, tmp_path):
        """With one executor shared by several children, a child dying breaks
        the executor as a whole, so a parse queued behind it fails with
        ``BrokenProcessPool`` (measured), and replacing that executor would kill
        the parse running beside the dead one. Per-slot executors fail only the
        dead child's own parse.

        Both parses signal from inside their child before the death, so the
        sibling is provably mid-parse when it happens."""
        pool = pymupdf_mod._get_pool()
        busy = asyncio.create_task(pool.run(_signal_then_wait, str(tmp_path / "busy"), str(tmp_path / "go")))
        dying = asyncio.create_task(pool.run(_signal_then_exit, str(tmp_path / "dying"), str(tmp_path / "die")))
        await _until_exists(tmp_path / "busy", tmp_path / "dying")
        waiting = asyncio.create_task(pool.run(os.getpid))
        await asyncio.sleep(0)  # both slots are taken, so it waits for one

        (tmp_path / "die").touch()
        with pytest.raises(concurrent.futures.process.BrokenProcessPool):
            await dying
        assert await waiting != os.getpid()
        (tmp_path / "go").touch()
        assert await busy != os.getpid()

    @pytest.mark.asyncio
    async def test_a_cancelled_parse_has_its_child_killed(self):
        """A parse abandoned by a timeout keeps running in its child; the slot
        must come back with a fresh child, not stay behind the old parse."""
        pymupdf_mod.configure_pool(pymupdf_mod.PyMuPDFPoolSettings(max_workers=1, memory_limit_mb=4096))
        pool = pymupdf_mod._get_pool()
        stuck_pid = await pool.run(os.getpid)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(pool.run(_sleep_then_pid, 60.0), timeout=1.0)

        fresh_pid = await asyncio.wait_for(pool.run(os.getpid), timeout=30.0)
        assert fresh_pid != stuck_pid
        assert not _process_alive(stuck_pid), "the abandoned parse's child is still running"

    @pytest.mark.asyncio
    async def test_a_child_is_replaced_after_max_tasks_per_child_parses(self):
        """Recycling releases memory a long-lived child never hands back."""
        pymupdf_mod.configure_pool(
            pymupdf_mod.PyMuPDFPoolSettings(max_workers=1, max_tasks_per_child=1, memory_limit_mb=4096)
        )
        pool = pymupdf_mod._get_pool()

        assert await pool.run(os.getpid) != await pool.run(os.getpid)

    @pytest.mark.asyncio
    async def test_reconfiguring_stops_the_old_pools_children(self):
        pool = pymupdf_mod._get_pool()
        old_pid = await pool.run(os.getpid)

        pymupdf_mod.configure_pool(pymupdf_mod.PyMuPDFPoolSettings(max_workers=3))
        deadline = time.monotonic() + 30
        while _process_alive(old_pid) and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert not _process_alive(old_pid), "the replaced pool's child is still running"

    @pytest.mark.asyncio
    async def test_reconfiguring_kills_a_parse_still_running_on_the_old_pool(self):
        """``shutdown()`` clears an executor's processes, so a child it only asked
        to exit could not be killed afterwards, by a cancellation or otherwise.
        The old pool now kills it, and retiring the executor again finds nothing
        left to kill instead of raising ``AttributeError``."""
        pool = pymupdf_mod._get_pool()
        stuck = asyncio.ensure_future(pool.run(_sleep_then_pid, 60.0))
        pids: list[int] = []
        deadline = time.monotonic() + 30
        while not pids and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
            pids = [pid for executor in pool._slots if executor is not None for pid in executor._processes or {}]
        assert pids, "the parse never got a child"

        pymupdf_mod.configure_pool(pymupdf_mod.PyMuPDFPoolSettings(max_workers=3))
        with pytest.raises(concurrent.futures.process.BrokenProcessPool):
            await asyncio.wait_for(stuck, timeout=30.0)
        assert not any(_process_alive(pid) for pid in pids), "the old pool's running parse outlived it"

    @pytest.mark.asyncio
    async def test_a_parse_waiting_on_a_replaced_pool_builds_no_child_there(self):
        """Nothing retires the old pool twice, so a child built in it after
        ``configure_pool`` replaced it would run for as long as the actor."""
        pymupdf_mod.configure_pool(pymupdf_mod.PyMuPDFPoolSettings(max_workers=1, memory_limit_mb=4096))
        pool = pymupdf_mod._get_pool()
        await pool.run(os.getpid)
        running = asyncio.ensure_future(pool.run(_sleep_then_pid, 60.0))
        waiting = asyncio.ensure_future(pool.run(os.getpid))
        await asyncio.sleep(0.5)

        pymupdf_mod.configure_pool(pymupdf_mod.PyMuPDFPoolSettings(max_workers=2))
        with pytest.raises(concurrent.futures.process.BrokenProcessPool, match="replaced while this parse ran"):
            await asyncio.wait_for(running, timeout=30.0)
        with pytest.raises(RuntimeError, match="replaced while this parse waited"):
            await asyncio.wait_for(waiting, timeout=30.0)
        assert pool._slots == [None]

    @pytest.mark.asyncio
    async def test_a_child_that_cannot_start_leaves_no_parse_queued(self, tmp_path, monkeypatch):
        """``submit()`` queues the parse before it starts the child. Kept, the
        failed file would run first in the next child, on the next file's time."""
        pymupdf_mod.configure_pool(pymupdf_mod.PyMuPDFPoolSettings(max_workers=1, memory_limit_mb=4096))
        _fail_next_spawns(monkeypatch, 1)
        pool = pymupdf_mod._get_pool()
        ran = tmp_path / "ran"

        with pytest.raises(BlockingIOError):
            await pool.run(_record, str(ran), "A")
        await asyncio.wait_for(pool.run(_record, str(ran), "B"), timeout=30.0)

        assert ran.read_text() == "B"

    @pytest.mark.asyncio
    async def test_a_recycled_child_that_cannot_start_fails_its_parse_at_once(self, monkeypatch):
        """Measured with the executor's own ``max_tasks_per_child``: the spawn
        failed on its thread, which died, and the next parse waited for the
        parse timeout."""
        pymupdf_mod.configure_pool(
            pymupdf_mod.PyMuPDFPoolSettings(max_workers=1, max_tasks_per_child=1, memory_limit_mb=4096)
        )
        # Installed first: the replacement can start as soon as the first parse
        # returns, before the test gets to run again.
        _fail_next_spawns(monkeypatch, 1, after=1)
        pool = pymupdf_mod._get_pool()
        first = await asyncio.wait_for(pool.run(os.getpid), timeout=30.0)
        # The next file arrives once the first child is gone, as between two
        # uploads; the executor's own thread would have tried its replacement.
        deadline = time.monotonic() + 30
        while _process_alive(first) and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.5)

        with pytest.raises(BlockingIOError):
            await asyncio.wait_for(pool.run(os.getpid), timeout=30.0)
        assert await asyncio.wait_for(pool.run(os.getpid), timeout=30.0) != first

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
        ctx = multiprocessing.get_context("spawn")
        with concurrent.futures.ProcessPoolExecutor(max_workers=1, mp_context=ctx) as pool:
            assert pool.submit(_bounded_mmap, 256).result(timeout=60) == "mapped"

    @pytest.mark.skipif(not sys.platform.startswith("linux"), reason="RLIMIT_DATA only covers mmap on Linux")
    def test_the_memory_ceiling_bounds_a_parse(self):
        """Run it for real: asserting ``setrlimit`` was called would pass just as
        well with ``RLIMIT_AS``, which would refuse the mappings a parse needs."""
        ctx = multiprocessing.get_context("spawn")
        with concurrent.futures.ProcessPoolExecutor(max_workers=1, mp_context=ctx) as pool:
            assert pool.submit(_bounded_alloc, 256, 1024).result(timeout=60) == "MemoryError"
            assert pool.submit(_bounded_alloc, 256, 32).result(timeout=60) == "allocated"


# ---------------------------------------------------------------------------
# What reaches the parent when a parse fails in its child
# ---------------------------------------------------------------------------

# Measured with PyMuPDF 1.26 under RLIMIT_DATA: the text MuPDF reports when the
# ceiling refuses one of its allocations.
_MUPDF_CALLOC_FAILURE = "code=2: calloc (18904 x 40 bytes) failed"
_MUPDF_FREETYPE_FAILURE = "FzErrorLibrary: code=3: FT_New_Memory_Face(): out of memory"
# Measured on JPEG 2000 images under RLIMIT_DATA: the two errors MuPDF raises
# for one it could not read, and what OpenJPEG logged to MuPDF's warnings.
_JPX_DECODE_FAILURE = "code=8: Failed to decode JPX image"
_JPX_HEADER_FAILURE = "code=8: Failed to read JPX header"
_JPX_TILE_TOO_LARGE = "openjpeg error: Size of tile data exceeds system limits"
_JPX_SIZ_NO_MEMORY = "openjpeg error: Not enough memory to take in charge SIZ marker"
_JPX_TILE_MEMORY_ERROR = "openjpeg error: Cannot decode tile, memory error"
_JPX_DAMAGED = "openjpeg error: Tile part length size inconsistent with stream length"


class _Unpicklable(Exception):
    """Stands in for MuPDF's SWIG exceptions, which hold an unpicklable handle."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.handle = threading.Lock()


def _raise_mupdf_alloc_failure(raw: bytes, filename: str):
    raise _Unpicklable(_MUPDF_CALLOC_FAILURE)


def _raise_bare_memory_error(raw: bytes, filename: str):
    raise MemoryError


def _raise_unpicklable(raw: bytes, filename: str):
    raise _Unpicklable("code=4: no font file for digest")


def _raise_system_error(raw: bytes, filename: str):
    raise SystemError("error return without exception set")


def _raise_jpx_failure(warning: str, message: str, raw: bytes, filename: str):
    """Fail as MuPDF does on a JPEG 2000 image, after OpenJPEG logged *warning*."""
    if warning:
        pymupdf.mupdf.fz_warn(warning)
    raise _Unpicklable(message)


def _raise_value_error(raw: bytes, filename: str):
    raise ValueError("not a PDF")


def _raise_chained_jpx_failure(raw: bytes, filename: str):
    try:
        raise _Unpicklable(_JPX_DECODE_FAILURE)
    except _Unpicklable as inner:
        raise ValueError("page 3 failed") from inner


def _raise_jpx_failure_while_handling(raw: bytes, filename: str):
    """Chained implicitly, as an error raised inside an ``except`` block is."""
    try:
        raise _Unpicklable(_JPX_DECODE_FAILURE)
    except _Unpicklable:
        raise ValueError("page 3 failed")  # noqa: B904 - the implicit chain is the point


def _raise_cyclic_chain(raw: bytes, filename: str):
    outer = ValueError("page 3 failed")
    inner = ValueError("page 4 failed")
    outer.__cause__, inner.__cause__ = inner, outer
    raise outer


def _jpx_pdf_declaring(directory, size: int):
    """A PDF whose JPEG 2000 image declares one *size* x *size* tile but holds a
    64 x 48 codestream, so decoding finds it damaged."""
    import io
    import struct

    from PIL import Image

    small = io.BytesIO()
    Image.new("RGB", (64, 48), (1, 2, 3)).save(small, format="JPEG2000", no_jp2=True)
    codestream = bytearray(small.getvalue())
    siz = codestream.index(b"\xff\x51")
    struct.pack_into(">IIIIII", codestream, siz + 6, size, size, 0, 0, size, size)
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Body text.")
    seed = io.BytesIO()
    Image.new("RGB", (64, 48)).save(seed, format="JPEG2000")
    xref = page.insert_image(pymupdf.Rect(72, 120, 523, 458), stream=seed.getvalue())
    doc.update_stream(xref, bytes(codestream), compress=False)
    doc.xref_set_key(xref, "Filter", "/JPXDecode")
    doc.xref_set_key(xref, "Width", str(size))
    doc.xref_set_key(xref, "Height", str(size))
    path = directory / "declares_more_than_it_holds.pdf"
    doc.save(path)
    doc.close()
    return path


def _forget_mupdf_warnings() -> None:
    """Flushed as well as reset, so no earlier warning can be counted as a repeat."""
    pymupdf.TOOLS.mupdf_warnings(reset=1)


def _release_state() -> tuple[bool, bool]:
    return pymupdf_mod._IN_POOL_CHILD, pymupdf_mod._MALLOC_TRIM is not None


def _bare_memory_error():
    raise MemoryError


class TestFailuresTheParentCanActOn:
    @pytest.fixture(autouse=True)
    def _limited_pool(self):
        pymupdf_mod.configure_pool(pymupdf_mod.PyMuPDFPoolSettings(max_workers=1, memory_limit_mb=4096))
        _forget_mupdf_warnings()
        yield
        _forget_mupdf_warnings()
        _drop_pool()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("extract", [_raise_mupdf_alloc_failure, _raise_bare_memory_error])
    async def test_a_ceiling_hit_arrives_as_a_memory_error_naming_the_setting(self, extract):
        """Before: MuPDF's error arrived as ``TypeError: cannot pickle
        'SwigPyObject' object``, and a bare ``MemoryError`` with no message."""
        with pytest.raises(
            MemoryError,
            match=r"exceeded PYMUPDF_PARSE_MEMORY_LIMIT_MB=4096 MiB\. Raise that limit to index this file\.$",
        ):
            await pymupdf_mod._get_pool().run(pymupdf_mod._run_extract, extract, b"", "x.pdf")

    @pytest.mark.asyncio
    async def test_a_ceiling_hit_replaces_the_child(self):
        """Freeing the parse's objects need not bring the child back under its
        ceiling; measured, the next, smaller file then failed in that child."""
        pool = pymupdf_mod._get_pool()
        before = await pool.run(os.getpid)
        with pytest.raises(MemoryError):
            await pool.run(pymupdf_mod._run_extract, _raise_bare_memory_error, b"", "x.pdf")

        assert await pool.run(os.getpid) != before

    @pytest.mark.asyncio
    async def test_an_unpicklable_error_arrives_with_its_own_text(self):
        with pytest.raises(RuntimeError, match=r"_Unpicklable: code=4: no font file for digest"):
            await pymupdf_mod._get_pool().run(pymupdf_mod._run_extract, _raise_unpicklable, b"", "x.pdf")

    @pytest.mark.asyncio
    async def test_a_child_that_dies_under_a_ceiling_names_the_setting(self):
        """A dead child arrives as a bare ``BrokenProcessPool``; under a ceiling,
        the ceiling is its likeliest cause (measured: the child can die receiving
        the next file, outside any parse code)."""
        with pytest.raises(MemoryError, match=r"child process died.*PYMUPDF_PARSE_MEMORY_LIMIT_MB=4096 MiB"):
            await pymupdf_mod._get_pool().run(os._exit, 9)

    @pytest.mark.asyncio
    async def test_a_child_killed_by_a_signal_is_not_blamed_on_the_ceiling(self):
        """The ceiling ends a child through a Python ``MemoryError``; a signal is a
        segfault or the kernel's OOM killer, where raising the limit is wrong."""
        with pytest.raises(concurrent.futures.process.BrokenProcessPool, match="killed by SIGKILL"):
            await pymupdf_mod._get_pool().run(_kill_self)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("exit_codes", "match"),
        [([None, None, None, -signal.SIGKILL], "killed by SIGKILL"), ([None], "terminated abruptly")],
        ids=["known-late", "never-known"],
    )
    async def test_a_dead_childs_exit_code_is_awaited_and_an_unknown_one_blames_nothing(
        self, monkeypatch, exit_codes, match
    ):
        """The pool fails the parse before its thread has joined the dead child,
        so its exit code can still read ``None``: a SIGKILL then came back as the
        ceiling's ``MemoryError`` (seen in 1 of 40 kills)."""

        class _LateExit:
            def __init__(self) -> None:
                self._codes = iter(exit_codes)

            @property
            def exitcode(self) -> int | None:
                return next(self._codes, exit_codes[-1])

        monkeypatch.setattr(pymupdf_mod, "_child_process", lambda executor: _LateExit())
        monkeypatch.setattr(pymupdf_mod, "_EXIT_CODE_WAIT_SECONDS", 0.2)
        with pytest.raises(concurrent.futures.process.BrokenProcessPool, match=match):
            await pymupdf_mod._get_pool().run(os._exit, 9)

    @pytest.mark.asyncio
    async def test_a_damaged_jpx_declaring_more_than_the_ceiling_says_it_may_be_damaged(self, tmp_path):
        """Measured: this image fails as damaged without a ceiling, but under any
        ceiling up to 16 GiB OpenJPEG is refused its tile before decoding reaches
        the damage, so raising the limit alone cannot help."""
        path = _jpx_pdf_declaring(tmp_path, 60000)
        with pytest.raises(MemoryError, match=r"PYMUPDF_PARSE_MEMORY_LIMIT_MB=4096 MiB\..* the image is damaged\.$"):
            await pymupdf_mod._get_pool().run(pymupdf_mod._run_extract, _extract_markdown, str(path), "x.pdf")

    @pytest.mark.asyncio
    async def test_without_a_ceiling_an_empty_memory_error_names_no_setting(self):
        pymupdf_mod.configure_pool(pymupdf_mod.PyMuPDFPoolSettings(max_workers=2))
        with pytest.raises(MemoryError, match=r"^PyMuPDF ran out of memory parsing this PDF\.$"):
            await pymupdf_mod._get_pool().run(_bare_memory_error)

    @pytest.mark.asyncio
    async def test_an_empty_memory_error_from_the_child_gets_its_message(self):
        """Measured under a tight ceiling: the child can fail to build the
        message too, and send back a ``MemoryError`` with none."""
        with pytest.raises(MemoryError, match=r"PYMUPDF_PARSE_MEMORY_LIMIT_MB=4096 MiB"):
            await pymupdf_mod._get_pool().run(_bare_memory_error)

    @pytest.mark.asyncio
    async def test_a_system_error_under_a_ceiling_is_out_of_memory(self):
        """Measured: out of memory, the binding can fail to build its own
        exception and raise ``SystemError`` instead. Under a ceiling that is the
        ceiling, and the child needs replacing like any other hit."""
        pool = pymupdf_mod._get_pool()
        before = await pool.run(os.getpid)
        with pytest.raises(MemoryError, match=r"PYMUPDF_PARSE_MEMORY_LIMIT_MB=4096 MiB"):
            await pool.run(pymupdf_mod._run_extract, _raise_system_error, b"", "x.pdf")

        assert await pool.run(os.getpid) != before

    def test_without_a_ceiling_a_system_error_crosses_unchanged(self, monkeypatch):
        monkeypatch.setattr(pymupdf_mod, "_CHILD_MEMORY_LIMIT_MB", 0)
        with pytest.raises(SystemError):
            pymupdf_mod._run_extract(_raise_system_error, b"", "x.pdf")

    @pytest.mark.parametrize("message", [_MUPDF_CALLOC_FAILURE, _MUPDF_FREETYPE_FAILURE], ids=["allocator", "FreeType"])
    def test_an_allocation_failure_is_recognised_behind_another_error(self, message):
        """A refused allocation deep in MuPDF can surface wrapped in whatever
        the caller raised; only the chain says what happened."""
        try:
            try:
                raise RuntimeError(message)
            except RuntimeError as inner:
                raise ValueError("page 3 failed") from inner
        except ValueError as outer:
            assert _is_allocation_failure(outer)

    def test_a_refused_ceiling_is_not_named_in_the_error(self, monkeypatch):
        """At or below the child's baseline the ceiling is not applied, so a
        failure there cannot be blamed on it."""
        monkeypatch.setattr(process_limits, "child_vmdata_mb", lambda: 10_000)
        monkeypatch.setattr(pymupdf_mod, "_CHILD_MEMORY_LIMIT_MB", 0)
        monkeypatch.setattr(pymupdf_mod, "_CHILD_REQUESTED_LIMIT_MB", 0)
        monkeypatch.setattr(pymupdf_mod, "_IN_POOL_CHILD", False)
        monkeypatch.setattr(pymupdf_mod, "_MALLOC_TRIM", None)
        pymupdf_mod._pool_worker_init(4096)

        assert pymupdf_mod._CHILD_MEMORY_LIMIT_MB == 0
        with pytest.raises(MemoryError, match=r"^PyMuPDF ran out of memory parsing this PDF\.$"):
            pymupdf_mod._run_extract(_raise_bare_memory_error, b"", "x.pdf")

    @pytest.mark.asyncio
    async def test_an_ordinary_error_crosses_unchanged(self):
        with pytest.raises(pymupdf.FileDataError):
            await PyMuPDFParser().parse(
                Document(filename="x.pdf", content_type=DocumentType.PDF, raw_bytes=b"%PDF-1.7\n" + b"\x00" * 512)
            )

    @pytest.mark.parametrize("message", [_MUPDF_CALLOC_FAILURE, _MUPDF_FREETYPE_FAILURE], ids=["allocator", "FreeType"])
    def test_an_allocation_failure_is_not_retried_on_a_cleaned_copy(self, message):
        """A cleaned copy needs the same memory again: the retry only doubles the
        work under the same ceiling before failing anyway."""
        with patch(_TO_MARKDOWN, side_effect=RuntimeError(message)) as to_markdown:
            with pytest.raises(MemoryError):
                pymupdf_mod._run_extract(_extract_markdown, _minimal_pdf_bytes(), "x.pdf")

        assert to_markdown.call_count == 1

    def test_without_a_ceiling_the_message_names_no_setting(self):
        with pytest.raises(MemoryError, match=r"^PyMuPDF ran out of memory parsing this PDF\.$"):
            pymupdf_mod._run_extract(_raise_bare_memory_error, b"", "x.pdf")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("warning", "message"),
        [
            (_JPX_TILE_TOO_LARGE, _JPX_DECODE_FAILURE),
            (_JPX_SIZ_NO_MEMORY, _JPX_HEADER_FAILURE),
            (_JPX_TILE_MEMORY_ERROR, _JPX_DECODE_FAILURE),
        ],
        ids=["decode", "header", "tile-memory-error"],
    )
    async def test_a_jpx_failure_logged_as_out_of_memory_is_the_ceiling(self, warning, message):
        """Measured: JPEG 2000 images that parse without a ceiling fail under one
        with these errors, which name no memory; OpenJPEG's warning does. Before,
        it was a plain error, and the child near its ceiling was kept."""
        pool = pymupdf_mod._get_pool()
        before = await pool.run(os.getpid)
        extract = functools.partial(_raise_jpx_failure, warning, message)
        with pytest.raises(
            MemoryError,
            match=r"PYMUPDF_PARSE_MEMORY_LIMIT_MB=4096 MiB\. Raise that limit to index this file\. It was a JPEG 2000 "
            r"image: if the file still fails with a higher limit, or without one, the image is damaged\.$",
        ):
            await pool.run(pymupdf_mod._run_extract, extract, b"", "x.pdf")

        assert await pool.run(os.getpid) != before

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "warning",
        [_JPX_DAMAGED, "Read from memory error. Got 3 bytes, block should be of 4 bytes", ""],
        ids=["damaged", "memory-not-refused", "nothing-logged"],
    )
    async def test_a_jpx_failure_not_logged_as_memory_names_both_causes(self, warning):
        """Measured: a damaged image fails with the same error, and a tiled image
        over the ceiling failed with no memory warning either. The error says
        both. The child is replaced: over the ceiling, it still held about 600 MiB of
        ``VmData`` after releasing the parse's memory, and the next file failed in
        it, though that file parsed in a fresh child (measured at 900 MiB)."""
        pool = pymupdf_mod._get_pool()
        before = await pool.run(os.getpid)
        extract = functools.partial(_raise_jpx_failure, warning, _JPX_DECODE_FAILURE)
        with pytest.raises(
            pymupdf_mod.JPEG2000ReadError,
            match=r"Failed to decode JPX image\. A JPEG 2000 image.*4096 MiB memory limit "
            r"\(PYMUPDF_PARSE_MEMORY_LIMIT_MB\).*higher limit, raise it;.*damaged",
        ):
            await pool.run(pymupdf_mod._run_extract, extract, b"", "x.pdf")

        assert await pool.run(os.getpid) != before

    @pytest.mark.parametrize(
        "fails", [_raise_chained_jpx_failure, _raise_jpx_failure_while_handling], ids=["from", "while-handling"]
    )
    def test_a_jpx_failure_is_recognised_behind_another_error(self, in_child, fails):
        with pytest.raises(pymupdf_mod.JPEG2000ReadError, match=r"^ValueError: page 3 failed\. A JPEG 2000 image"):
            pymupdf_mod._run_extract(fails, b"", "x.pdf")

    @pytest.mark.parametrize(
        "warning",
        [
            "openjpeg error: Cannot allocate Tier 1 handle",
            "openjpeg error: opj_j2k_read_tlm(): cannot allocate m_tile_part_infos.",
            "openjpeg error: Error allocating tile component data.",
            "openjpeg error: Memory allocation failure in opj_jp2_apply_pclr().",
            "openjpeg error: opj_j2k_build_tp_index_from_tlm(): tile index allocation failed",
        ],
    )
    def test_openjpeg_s_other_allocation_failures_are_the_ceiling(self, in_child, warning):
        """OpenJPEG's other messages for a refused allocation, from the bundled library's strings."""
        extract = functools.partial(_raise_jpx_failure, warning, _JPX_DECODE_FAILURE)
        with pytest.raises(MemoryError, match=r"PYMUPDF_PARSE_MEMORY_LIMIT_MB=4096 MiB"):
            pymupdf_mod._run_extract(extract, b"", "x.pdf")

    def test_a_cyclic_chain_is_walked_once(self, in_child):
        with pytest.raises(ValueError, match="page 3 failed"):
            pymupdf_mod._run_extract(_raise_cyclic_chain, b"", "x.pdf")

    def test_without_a_ceiling_a_jpx_failure_crosses_unchanged(self, monkeypatch):
        monkeypatch.setattr(pymupdf_mod, "_CHILD_MEMORY_LIMIT_MB", 0)
        extract = functools.partial(_raise_jpx_failure, _JPX_TILE_TOO_LARGE, _JPX_DECODE_FAILURE)
        with pytest.raises(RuntimeError, match=r"^_Unpicklable: code=8: Failed to decode JPX image$"):
            pymupdf_mod._run_extract(extract, b"", "x.pdf")

    def test_another_decode_failure_under_a_ceiling_is_not_jpx(self, in_child):
        extract = functools.partial(_raise_jpx_failure, _JPX_TILE_TOO_LARGE, "code=8: Failed to decode JBIG2 image")
        with pytest.raises(RuntimeError, match=r"^_Unpicklable: code=8: Failed to decode JBIG2 image$"):
            pymupdf_mod._run_extract(extract, b"", "x.pdf")

    def test_an_earlier_parse_warning_does_not_count(self, in_child):
        """MuPDF's warnings outlive a parse; only this parse's may decide."""
        pymupdf.mupdf.fz_warn(_JPX_TILE_TOO_LARGE)
        extract = functools.partial(_raise_jpx_failure, "", _JPX_DECODE_FAILURE)
        with pytest.raises(pymupdf_mod.JPEG2000ReadError, match="damaged"):
            pymupdf_mod._run_extract(extract, b"", "x.pdf")

    def test_a_warning_repeating_the_last_one_still_counts(self, in_child):
        """MuPDF logs a warning identical to the last one only as a count, so a
        parse whose warning repeats the previous parse's last one would otherwise
        log just "... repeated 2 times..."."""
        pymupdf.mupdf.fz_warn(_JPX_TILE_TOO_LARGE)
        extract = functools.partial(_raise_jpx_failure, _JPX_TILE_TOO_LARGE, _JPX_DECODE_FAILURE)
        with pytest.raises(MemoryError, match=r"PYMUPDF_PARSE_MEMORY_LIMIT_MB=4096 MiB"):
            pymupdf_mod._run_extract(extract, b"", "x.pdf")

    def test_a_jpx_failure_names_the_ceiling_the_child_applied(self, in_child, monkeypatch):
        monkeypatch.setattr(pymupdf_mod, "_CHILD_MEMORY_LIMIT_MB", 777)
        monkeypatch.setattr(pymupdf_mod, "_CHILD_REQUESTED_LIMIT_MB", 777)
        extract = functools.partial(_raise_jpx_failure, "", _JPX_DECODE_FAILURE)
        with pytest.raises(pymupdf_mod.JPEG2000ReadError, match=r"777 MiB memory limit"):
            pymupdf_mod._run_extract(extract, b"", "x.pdf")

    def test_a_memory_error_while_classifying_is_out_of_memory(self, in_child, monkeypatch):
        """Under a tight ceiling, reading the warnings can itself be refused memory."""

        def refused(exc):
            raise MemoryError

        monkeypatch.setattr(pymupdf_mod, "_is_allocation_failure", refused)
        with pytest.raises(MemoryError, match=r"PYMUPDF_PARSE_MEMORY_LIMIT_MB=4096 MiB"):
            pymupdf_mod._run_extract(_raise_value_error, b"", "x.pdf")

    @pytest.mark.parametrize(
        ("fails", "error", "message"),
        [
            (
                _raise_bare_memory_error,
                MemoryError,
                r"512 MiB limit, which a limit it inherited lowered from PYMUPDF_PARSE_MEMORY_LIMIT_MB=1024 MiB\. "
                r"Raise the inherited limit to index this file\.$",
            ),
            (
                functools.partial(_raise_jpx_failure, "", _JPX_DECODE_FAILURE),
                pymupdf_mod.JPEG2000ReadError,
                r"512 MiB memory limit, which a limit it inherited lowered from PYMUPDF_PARSE_MEMORY_LIMIT_MB=1024 MiB, "
                r"the cause can be that limit.*higher limit, raise the inherited limit;",
            ),
        ],
        ids=["out-of-memory", "jpx"],
    )
    def test_a_lowered_ceiling_names_the_limit_that_lowered_it(self, monkeypatch, fails, error, message):
        """Raising the setting cannot help there: the child never goes above an
        inherited limit. Through the child's real startup, with an inherited
        512 MiB limit and 1024 asked for."""
        monkeypatch.setattr(resource, "getrlimit", lambda which: (512 * 1024 * 1024, resource.RLIM_INFINITY))
        monkeypatch.setattr(resource, "setrlimit", lambda which, limits: None)
        monkeypatch.setattr(process_limits, "child_vmdata_mb", lambda: 60)
        monkeypatch.setattr(pymupdf_mod, "_CHILD_MEMORY_LIMIT_MB", 0)
        monkeypatch.setattr(pymupdf_mod, "_CHILD_REQUESTED_LIMIT_MB", 0)
        monkeypatch.setattr(pymupdf_mod, "_IN_POOL_CHILD", False)
        monkeypatch.setattr(pymupdf_mod, "_MALLOC_TRIM", None)
        pymupdf_mod._pool_worker_init(1024)
        monkeypatch.setattr(pymupdf_mod, "_MALLOC_TRIM", None)
        monkeypatch.setattr(pymupdf_mod.pymupdf.TOOLS, "store_shrink", lambda percent: None)

        with pytest.raises(error, match=message):
            pymupdf_mod._run_extract(fails, b"", "x.pdf")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("fails", [_bare_memory_error, functools.partial(os._exit, 9)], ids=["empty", "died"])
    async def test_the_parent_hedges_the_ceiling_it_names(self, fails):
        """The parent names the configured ceiling, which the child may have
        refused (at or below its baseline) or lowered (an inherited limit)."""
        with pytest.raises(
            MemoryError,
            match=r"likeliest.*raise it to index this file\. If the child logged at startup that a limit it inherited "
            r"lowered that one, raise the inherited limit instead; if it logged that it did not apply it, "
            r"PYMUPDF_PARSE_MEMORY_LIMIT_MB is not the cause\.$",
        ):
            await pymupdf_mod._get_pool().run(fails)


@pytest.fixture
def in_child(monkeypatch):
    """This process as a pool child under a 4096 MiB ceiling, without its housekeeping."""
    monkeypatch.setattr(pymupdf_mod, "_IN_POOL_CHILD", True)
    monkeypatch.setattr(pymupdf_mod, "_CHILD_MEMORY_LIMIT_MB", 4096)
    monkeypatch.setattr(pymupdf_mod, "_CHILD_REQUESTED_LIMIT_MB", 4096)
    monkeypatch.setattr(pymupdf_mod, "_MALLOC_TRIM", None)
    monkeypatch.setattr(pymupdf_mod.pymupdf.TOOLS, "store_shrink", lambda percent: None)
    _forget_mupdf_warnings()
    yield
    _forget_mupdf_warnings()


class TestParseMemoryIsReleased:
    @pytest.fixture
    def calls(self, monkeypatch) -> list[str]:
        calls: list[str] = []
        monkeypatch.setattr(pymupdf_mod, "_IN_POOL_CHILD", True)
        monkeypatch.setattr(pymupdf_mod.pymupdf.TOOLS, "store_shrink", lambda percent: calls.append(f"store {percent}"))
        monkeypatch.setattr(pymupdf_mod, "_MALLOC_TRIM", lambda pad: calls.append(f"trim {pad}"))
        return calls

    def test_a_child_hands_back_memory_after_a_parse(self, calls):
        def extract(raw: bytes, filename: str):
            calls.append("parse")
            return [], []

        pymupdf_mod._run_extract(extract, b"", "x.pdf")

        assert calls == ["parse", "store 100", "trim 0"]

    def test_a_child_hands_back_memory_after_a_failed_parse(self, calls):
        def extract(raw: bytes, filename: str):
            calls.append("parse")
            raise ValueError("not a PDF")

        with pytest.raises(ValueError, match="not a PDF"):
            pymupdf_mod._run_extract(extract, b"", "x.pdf")

        assert calls == ["parse", "store 100", "trim 0"]

    def test_a_child_over_its_ceiling_is_not_trimmed(self, calls):
        """It is replaced; only the store is emptied, so the message can be built."""

        def extract(raw: bytes, filename: str):
            calls.append("parse")
            raise MemoryError

        with pytest.raises(MemoryError):
            pymupdf_mod._run_extract(extract, b"", "x.pdf")

        assert calls == ["parse", "store 100"]

    def test_a_child_with_an_unreadable_jpx_image_is_not_trimmed(self, calls, monkeypatch):
        """It is replaced too: it may be over its ceiling."""
        monkeypatch.setattr(pymupdf_mod, "_CHILD_MEMORY_LIMIT_MB", 4096)

        def extract(raw: bytes, filename: str):
            calls.append("parse")
            raise _Unpicklable(_JPX_DECODE_FAILURE)

        with pytest.raises(pymupdf_mod.JPEG2000ReadError):
            pymupdf_mod._run_extract(extract, b"", "x.pdf")

        assert calls == ["parse"]

    def test_malloc_trim_is_looked_up_before_the_ceiling(self, monkeypatch):
        """Past the ceiling, loading it could fail the initializer."""
        order: list[str] = []
        monkeypatch.setattr(pymupdf_mod, "_load_malloc_trim", lambda: order.append("load") or None)
        monkeypatch.setattr(pymupdf_mod, "apply_parse_memory_limit", lambda *args, **kwargs: order.append("limit") or 0)
        monkeypatch.setattr(pymupdf_mod, "_CHILD_MEMORY_LIMIT_MB", 0)
        monkeypatch.setattr(pymupdf_mod, "_CHILD_REQUESTED_LIMIT_MB", 0)
        monkeypatch.setattr(pymupdf_mod, "_IN_POOL_CHILD", False)
        monkeypatch.setattr(pymupdf_mod, "_MALLOC_TRIM", None)
        pymupdf_mod._pool_worker_init(4096)

        assert order == ["load", "limit"]

    @pytest.mark.skipif(platform.libc_ver()[0] != "glibc", reason="malloc_trim is glibc's")
    def test_malloc_trim_is_called_with_its_c_signature(self):
        """Without it, ctypes passes the pad as a C int, where ``size_t`` is wider."""
        trim = pymupdf_mod._load_malloc_trim()

        assert (trim.argtypes, trim.restype) == ([ctypes.c_size_t], ctypes.c_int)

    def test_housekeeping_cannot_fail_the_parse(self, monkeypatch):
        def fail(pad):
            raise MemoryError

        monkeypatch.setattr(pymupdf_mod, "_IN_POOL_CHILD", True)
        monkeypatch.setattr(pymupdf_mod, "_MALLOC_TRIM", fail)

        assert pymupdf_mod._run_extract(lambda raw, filename: (["text"], []), b"", "x.pdf") == (["text"], [])
        with pytest.raises(ValueError, match="not a PDF"):
            pymupdf_mod._run_extract(_raise_value_error, b"", "x.pdf")

    def test_the_thread_leaves_its_process_memory_alone(self, calls, monkeypatch):
        """The thread shares its process with the rest of the worker."""
        monkeypatch.setattr(pymupdf_mod, "_IN_POOL_CHILD", False)
        pymupdf_mod._run_extract(lambda raw, filename: ([], []), b"", "x.pdf")

        assert calls == []

    @pytest.mark.asyncio
    async def test_a_real_child_finds_malloc_trim_on_glibc(self):
        pymupdf_mod.configure_pool(pymupdf_mod.PyMuPDFPoolSettings(max_workers=2))
        try:
            in_child, has_trim = await pymupdf_mod._get_pool().run(_release_state)
        finally:
            _drop_pool()

        assert in_child
        assert has_trim == (platform.libc_ver()[0] == "glibc")


def _echo(value):
    return value


def _drop_pool() -> None:
    """Back to the default pool, with the current pool's children and threads gone.

    ``configure_pool`` shuts the old executors down without waiting, so their
    manager threads can outlive the test that built them, and a later test that
    forks copies whatever lock one of those threads holds. The children are
    killed first so a parse still running cannot hold up the join, then one
    ``shutdown(wait=True)`` joins the manager thread: after a
    ``shutdown(wait=False)`` there is no thread left to join.
    """
    pool = pymupdf_mod._POOL
    for executor in [] if pool is None else pool._slots:
        if executor is not None:
            for process in list(getattr(executor, "_processes", {}).values()):
                process.kill()
            executor.shutdown(wait=True, cancel_futures=True)
    pymupdf_mod._POOL = None
    pymupdf_mod._POOL_SETTINGS = pymupdf_mod.PyMuPDFPoolSettings()


async def _until_exists(*paths, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while not all(path.exists() for path in paths):
        assert time.monotonic() < deadline, f"never appeared: {[str(p) for p in paths if not p.exists()]}"
        await asyncio.sleep(0.02)


def _signal_then_wait(started: str, release: str) -> int:
    """Say this parse is running, then hold it until *release* exists."""
    open(started, "w").close()
    deadline = time.monotonic() + 60
    while not os.path.exists(release) and time.monotonic() < deadline:
        time.sleep(0.02)
    return os.getpid()


def _signal_then_exit(started: str, release: str) -> None:
    _signal_then_wait(started, release)
    os._exit(9)


def _kill_self() -> None:
    os.kill(os.getpid(), signal.SIGKILL)


def _record(path: str, tag: str) -> int:
    with open(path, "a") as ran:
        ran.write(tag)
    return os.getpid()


def _fail_next_spawns(monkeypatch, count: int, after: int = 0) -> None:
    """Make *count* child starts fail, as in a pod out of process slots, once
    *after* have succeeded."""
    spawn = concurrent.futures.ProcessPoolExecutor._spawn_process
    starts = [0]

    def failing(executor):
        starts[0] += 1
        if after < starts[0] <= after + count:
            raise BlockingIOError(11, "Resource temporarily unavailable")
        spawn(executor)

    monkeypatch.setattr(concurrent.futures.ProcessPoolExecutor, "_spawn_process", failing)


def _sleep_then_pid(seconds: float) -> int:
    import time

    time.sleep(seconds)
    return os.getpid()


def _process_alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as stat:
            return stat.read().split()[2] != "Z"
    except FileNotFoundError:
        return False


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


class TestPagesWithFallback:
    """Per-page plain-text fallback when pymupdf4llm drops text (issue #1102)."""

    def _pdf_with_text(self, text: str) -> bytes:
        doc = pymupdf.open()
        try:
            page = doc.new_page()
            page.insert_text((72, 72), text)
            return doc.tobytes()
        finally:
            doc.close()

    def test_keeps_markdown_when_text_is_retained(self):
        """No fallback when markdown length is at least the threshold fraction."""
        raw = self._pdf_with_text("hello world")
        with pymupdf.open(stream=raw, filetype="pdf") as doc:
            chunks = [{"text": "hello world"}]
            pages = _pages_with_fallback(doc, chunks, "ok.pdf")
        assert pages == ["hello world"]

    def test_falls_back_to_plain_text_when_markdown_is_nearly_empty(self):
        """Markdown returning almost no text is replaced with page.get_text()."""
        raw = self._pdf_with_text("hello world this is a longer sentence to exceed the threshold")
        with pymupdf.open(stream=raw, filetype="pdf") as doc:
            plain_text = doc[0].get_text().strip()
            # Simulate pymupdf4llm returning nearly nothing
            chunks = [{"text": "x"}]
            pages = _pages_with_fallback(doc, chunks, "indesign.pdf")
        assert pages == [plain_text]

    def test_no_fallback_when_plain_text_is_empty(self):
        """Pages with no text layer at all (e.g. scanned image pages) keep the empty markdown result."""
        doc = pymupdf.open()
        try:
            doc.new_page()
            raw = doc.tobytes()
        finally:
            doc.close()

        with pymupdf.open(stream=raw, filetype="pdf") as doc:
            chunks = [{"text": ""}]
            pages = _pages_with_fallback(doc, chunks, "scanned.pdf")
        assert pages == [""]

    def test_mixed_pages_fallback_only_on_affected_pages(self):
        """Only the pages that drop below the threshold are replaced; good pages keep their markdown."""
        pdf_doc = pymupdf.open()
        try:
            page0 = pdf_doc.new_page()
            page0.insert_text((72, 72), "hello world this is a long sentence")
            page1 = pdf_doc.new_page()
            page1.insert_text((72, 72), "another page with text")
            raw = pdf_doc.tobytes()
        finally:
            pdf_doc.close()

        with pymupdf.open(stream=raw, filetype="pdf") as doc:
            plain0 = doc[0].get_text().strip()
            good_md1 = "another page with text"
            # Page 0 gets almost no markdown, page 1 gets full markdown
            chunks = [{"text": "z"}, {"text": good_md1}]
            pages = _pages_with_fallback(doc, chunks, "mixed.pdf")

        assert pages[0] == plain0
        assert pages[1] == good_md1

    def test_threshold_constant_is_below_one(self):
        """Sanity check: the threshold must be a fraction in (0, 1)."""
        assert 0 < _MARKDOWN_TEXT_RATIO_THRESHOLD < 1


class TestExtractMarkdownPerPageFallback:
    """Integration: _extract_markdown uses per-page fallback via _pages_with_fallback."""

    def test_falls_back_per_page_without_raising(self):
        """When pymupdf4llm silently drops text, _extract_markdown recovers it."""
        pdf_doc = pymupdf.open()
        try:
            page = pdf_doc.new_page()
            page.insert_text((72, 72), "important document content that must not be lost")
            raw = pdf_doc.tobytes()
        finally:
            pdf_doc.close()

        # Simulate pymupdf4llm returning nearly empty text for the page
        empty_chunks = [{"text": ""}]
        with patch(_TO_MARKDOWN, return_value=empty_chunks):
            pages, images = _extract_markdown(raw, "indesign_cs3.pdf")

        assert images == []
        assert len(pages) == 1
        # The fallback should have recovered the text layer content
        assert len(pages[0]) > 5

    @pytest.mark.asyncio
    async def test_parser_does_not_return_empty_blocks_when_text_layer_has_content(self):
        """End-to-end: PyMuPDFParser recovers non-empty text when pymupdf4llm drops it."""
        pdf_doc = pymupdf.open()
        try:
            page = pdf_doc.new_page()
            page.insert_text((72, 72), "important content")
            raw = pdf_doc.tobytes()
        finally:
            pdf_doc.close()

        document = Document(filename="lossy.pdf", content_type=DocumentType.PDF, raw_bytes=raw)
        empty_chunks = [{"text": ""}]
        with patch(_TO_MARKDOWN, return_value=empty_chunks):
            result = await PyMuPDFParser().parse(document)

        assert result.page_count == 1
        assert result.text_blocks[0].text != ""
