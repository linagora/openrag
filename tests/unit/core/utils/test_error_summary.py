import pytest
from core.utils.error_summary import (
    extract_task_error_reason,
    failure_reason_from_exception,
    summarize_task_error,
)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            'Traceback (most recent call last):\n  File "worker.py", line 1\n'
            "RuntimeError: upstream returned HTML\n<html>\n</html>",
            "RuntimeError: upstream returned HTML",
        ),
        (
            "Traceback (most recent call last):\nValueError: invalid document\n"
            "The parser rejected the document after validation.",
            "ValueError: invalid document",
        ),
        (
            "  + Exception Group Traceback (most recent call last):\n"
            "  | ExceptionGroup: batch failed (2 sub-exceptions)\n"
            "  +------------------------------------",
            "ExceptionGroup: batch failed (2 sub-exceptions)",
        ),
        (
            "pydantic_core._pydantic_core.ValidationError: 1 validation error for Document\n"
            "title\n  Field required [type=missing]",
            "ValidationError: 1 validation error for Document",
        ),
        (
            "Indexer worker submission was rejected after the worker settled.",
            "Indexer worker submission was rejected after the worker settled.",
        ),
        (
            "Traceback (most recent call last):\nDocumentRejected: unsupported content\n"
            "The document cannot be retried.",
            "DocumentRejected: unsupported content",
        ),
        (
            "Traceback (most recent call last):\nRuntimeError: upstream failed\nServer: nginx",
            "RuntimeError: upstream failed",
        ),
        (
            'Traceback (most recent call last):\n  File "worker.py", line 1, in run\nRuntimeError',
            "RuntimeError",
        ),
        (
            "Traceback (most recent call last):\nValueError: inner failure\n\n"
            "The above exception was the direct cause of the following exception:\n\n"
            "Traceback (most recent call last):\nRuntimeError: outer failure",
            "RuntimeError: outer failure",
        ),
    ],
)
def test_extract_task_error_reason_uses_exception_header(error: str, expected: str) -> None:
    assert extract_task_error_reason(error) == expected


def test_failure_reason_from_exception_uses_first_meaningful_line() -> None:
    exc = RuntimeError("\nupstream returned HTML\n<html>\n</html>")

    assert failure_reason_from_exception(exc) == "RuntimeError: upstream returned HTML"


def test_failure_reason_from_exception_handles_empty_messages() -> None:
    assert failure_reason_from_exception(RuntimeError()) == "RuntimeError"


def test_summary_prefers_stored_reason_and_caps_only_the_list_value() -> None:
    reason = "RuntimeError: " + "x" * 600

    summary = summarize_task_error("ValueError: legacy", reason=reason)

    assert summary is not None
    assert len(summary) == 500
    assert summary.endswith("...")


@pytest.mark.parametrize("error", [None, "", "Traceback (most recent call last):\n"])
def test_extract_task_error_reason_returns_none_without_an_exception_header(error: str | None) -> None:
    assert extract_task_error_reason(error) is None
