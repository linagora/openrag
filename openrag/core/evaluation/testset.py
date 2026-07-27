"""Parsing and validation of the uploaded test-set CSV.

The admin uploads a plain CSV; everything promptfoo needs is derived from it
so operators never have to learn promptfoo's YAML. Validation is strict and
reports the offending 1-based row numbers, because a malformed test set must
fail at upload time rather than half-way through a run that has already
indexed a corpus.
"""

from __future__ import annotations

import csv
import io

from core.models.evaluation import EvalTestCase
from core.utils.exceptions import ValidationError

QUERY_COLUMN = "question"
ANSWER_COLUMN = "expected_answer"
FILE_IDS_COLUMN = "expected_file_ids"

REQUIRED_COLUMNS = (QUERY_COLUMN, ANSWER_COLUMN)
OPTIONAL_COLUMNS = (FILE_IDS_COLUMN,)

#: ``expected_file_ids`` holds several ids in one cell, separated by this.
FILE_ID_SEPARATOR = ";"

#: Row numbers are reported to the user, so cap how many we list at once.
_MAX_REPORTED_ERRORS = 10


def _decode(raw: bytes) -> str:
    """Decode the upload, tolerating a UTF-8 BOM from Excel exports."""
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValidationError(
            "Test set must be UTF-8 encoded CSV.",
            code="EVAL_TESTSET_ENCODING",
            status_code=400,
        ) from exc


def _split_file_ids(cell: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in cell.split(FILE_ID_SEPARATOR) if part.strip())


def parse_testset(raw: bytes | str, *, max_rows: int) -> list[EvalTestCase]:
    """Parse the CSV upload into test cases.

    Args:
        raw: Raw upload bytes, or already-decoded text.
        max_rows: Reject test sets longer than this (``EVAL_MAX_TESTSET_ROWS``).
            Every row costs a retrieval call plus a graded generation per run,
            so the cap is a deployment concern rather than a fixed limit.

    Returns:
        One :class:`EvalTestCase` per data row, in file order.

    Raises:
        ValidationError: On a missing/duplicated header, an empty file, a row
            with a blank required cell, or more than ``max_rows`` rows.
    """
    text = _decode(raw) if isinstance(raw, bytes) else raw
    reader = csv.DictReader(io.StringIO(text))

    if reader.fieldnames is None:
        raise ValidationError(
            "Test set is empty — expected a CSV header row.",
            code="EVAL_TESTSET_EMPTY",
            status_code=400,
        )

    headers = [(name or "").strip().lower() for name in reader.fieldnames]
    missing = [column for column in REQUIRED_COLUMNS if column not in headers]
    if missing:
        raise ValidationError(
            f"Test set is missing required column(s): {', '.join(missing)}. "
            f"Expected header: {','.join((*REQUIRED_COLUMNS, *OPTIONAL_COLUMNS))}",
            code="EVAL_TESTSET_COLUMNS",
            status_code=400,
        )
    if len(set(headers)) != len(headers):
        raise ValidationError(
            "Test set has duplicate column names.",
            code="EVAL_TESTSET_COLUMNS",
            status_code=400,
        )

    # DictReader keys off the raw header spelling; normalise so " Question "
    # and "question" both resolve.
    key_for = {column: reader.fieldnames[headers.index(column)] for column in headers if column}

    cases: list[EvalTestCase] = []
    errors: list[str] = []

    for offset, row in enumerate(reader):
        # +2: one for the header line, one to make it 1-based like a spreadsheet.
        line = offset + 2
        query = (row.get(key_for[QUERY_COLUMN]) or "").strip()
        expected = (row.get(key_for[ANSWER_COLUMN]) or "").strip()

        if not query and not expected:
            continue  # blank trailing line
        if not query:
            errors.append(f"row {line}: '{QUERY_COLUMN}' is empty")
            continue
        if not expected:
            errors.append(f"row {line}: '{ANSWER_COLUMN}' is empty")
            continue

        file_ids_key = key_for.get(FILE_IDS_COLUMN)
        file_ids = _split_file_ids(row.get(file_ids_key) or "") if file_ids_key else ()
        cases.append(EvalTestCase(query=query, expected_answer=expected, expected_file_ids=file_ids))

    if errors:
        shown = errors[:_MAX_REPORTED_ERRORS]
        suffix = f" (+{len(errors) - len(shown)} more)" if len(errors) > len(shown) else ""
        raise ValidationError(
            "Test set has invalid rows — " + "; ".join(shown) + suffix,
            code="EVAL_TESTSET_ROWS",
            status_code=400,
        )
    if not cases:
        raise ValidationError(
            "Test set contains no usable rows.",
            code="EVAL_TESTSET_EMPTY",
            status_code=400,
        )
    if len(cases) > max_rows:
        raise ValidationError(
            f"Test set has {len(cases)} rows; the maximum is {max_rows}.",
            code="EVAL_TESTSET_TOO_LARGE",
            status_code=400,
        )
    return cases


__all__ = [
    "ANSWER_COLUMN",
    "FILE_IDS_COLUMN",
    "FILE_ID_SEPARATOR",
    "QUERY_COLUMN",
    "REQUIRED_COLUMNS",
    "parse_testset",
]
