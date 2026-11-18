from __future__ import annotations

import asyncio

import pytest
from core.indexing.parsers.pdf.pymupdf import PyMuPDFPoolSettings, _ParsePool
from core.indexing.parsers.progress import bind_parser_progress, report_parser_substage
from services.workers.stage_tracker import StageTracker


@pytest.mark.asyncio
async def test_parser_progress_from_worker_thread_is_marshaled_to_tracker_loop() -> None:
    tracker = StageTracker("task-1")
    tracker.start_stage("parse", 0)

    with bind_parser_progress(tracker):
        await asyncio.to_thread(
            report_parser_substage,
            "parse.text",
            "Extracting page text",
            progress=42,
            total=578,
        )
        await asyncio.sleep(0)

    summary = tracker.summary()
    assert summary.current_substage_id == "parse.text"
    assert summary.current_substage_label == "Extracting page text"
    assert summary.current_substage_progress == 42
    assert summary.current_substage_total == 578


@pytest.mark.asyncio
async def test_pymupdf_thread_pool_carries_parser_progress_context() -> None:
    tracker = StageTracker("task-1")
    tracker.start_stage("parse", 0)
    pool = _ParsePool(PyMuPDFPoolSettings())

    def report_from_parse_thread() -> None:
        report_parser_substage("parse.text", "Extracting page text", progress=2, total=3)

    try:
        with bind_parser_progress(tracker):
            await pool.run(report_from_parse_thread)
    finally:
        pool.shutdown()

    assert tracker.summary().current_substage_progress == 2
    assert tracker.summary().current_substage_total == 3


def test_parser_progress_is_optional_outside_an_indexing_scope() -> None:
    report_parser_substage("parse.text", "Extracting page text", progress=1, total=2)
