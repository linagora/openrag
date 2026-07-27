"""Tests for the evaluation test-set CSV parser."""

from __future__ import annotations

import pytest
from core.evaluation.testset import parse_testset
from core.utils.exceptions import ValidationError

#: The cap is deployment config (EVAL_MAX_TESTSET_ROWS); these tests pin
#: their own so they stay independent of the shipped default.
MAX_ROWS = 500

VALID = (
    "question,expected_answer,expected_file_ids\n"
    "What is the refund window?,30 days,policy.pdf\n"
    "Who approves large spend?,The CFO,finance.pdf;approvals.pdf\n"
)


def test_parses_rows_and_splits_file_ids():
    cases = parse_testset(VALID, max_rows=MAX_ROWS)
    assert [case.query for case in cases] == [
        "What is the refund window?",
        "Who approves large spend?",
    ]
    assert cases[0].expected_file_ids == ("policy.pdf",)
    assert cases[1].expected_file_ids == ("finance.pdf", "approvals.pdf")


def test_expected_file_ids_column_is_optional():
    """Answer-quality-only test sets are legitimate — the ranking metrics
    just report them as skipped."""
    cases = parse_testset("question,expected_answer\nWhy?,Because\n", max_rows=MAX_ROWS)
    assert cases[0].expected_file_ids == ()
    assert cases[0].has_ground_truth_sources is False


def test_accepts_bytes_with_utf8_bom():
    """Excel writes a BOM; decoding with plain utf-8 would corrupt the first
    header and make the required-column check fail."""
    cases = parse_testset(VALID.encode("utf-8-sig"), max_rows=MAX_ROWS)
    assert len(cases) == 2


def test_header_case_and_whitespace_are_normalised():
    cases = parse_testset(" Question , Expected_Answer \nWhy?,Because\n", max_rows=MAX_ROWS)
    assert cases[0].query == "Why?"
    assert cases[0].expected_answer == "Because"


def test_blank_trailing_lines_are_ignored():
    cases = parse_testset(VALID + ",\n\n", max_rows=MAX_ROWS)
    assert len(cases) == 2


def test_missing_required_column_is_rejected():
    with pytest.raises(ValidationError) as excinfo:
        parse_testset("question,answer\nWhy?,Because\n", max_rows=MAX_ROWS)
    assert "expected_answer" in str(excinfo.value)


def test_empty_required_cell_reports_the_spreadsheet_row_number():
    """Row 3 = second data row, counting the header as row 1."""
    with pytest.raises(ValidationError) as excinfo:
        parse_testset("question,expected_answer\nWhy?,Because\n,Orphan answer\n", max_rows=MAX_ROWS)
    assert "row 3" in str(excinfo.value)


def test_empty_file_is_rejected():
    with pytest.raises(ValidationError):
        parse_testset("", max_rows=MAX_ROWS)


def test_header_only_file_is_rejected():
    with pytest.raises(ValidationError):
        parse_testset("question,expected_answer\n", max_rows=MAX_ROWS)


def test_duplicate_columns_are_rejected():
    with pytest.raises(ValidationError):
        parse_testset("question,question,expected_answer\na,b,c\n", max_rows=MAX_ROWS)


def test_row_cap_is_enforced():
    rows = "".join(f"q{i},a{i}\n" for i in range(MAX_ROWS + 1))
    with pytest.raises(ValidationError) as excinfo:
        parse_testset("question,expected_answer\n" + rows, max_rows=MAX_ROWS)
    assert str(MAX_ROWS) in str(excinfo.value)


def test_invalid_encoding_is_rejected():
    with pytest.raises(ValidationError):
        parse_testset(b"\xff\xfe\x00question", max_rows=MAX_ROWS)
