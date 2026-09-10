"""Unit tests for :mod:`core.prompts.calendar_anchors`."""

from __future__ import annotations

from datetime import datetime

import pytest
from core.prompts.calendar_anchors import calendar_anchors

# Thursday, in the middle of a week / month / year — the case that surfaced
# the bug (Mistral resolved "last week" to the past 7 days on this date).
THURSDAY = datetime(2026, 9, 10, 10, 23, 21)


def _line(anchors: str, prefix: str) -> str:
    for line in anchors.splitlines():
        if line.startswith(prefix):
            return line
    raise AssertionError(f"no line starting with {prefix!r} in:\n{anchors}")


def test_week_anchors_are_monday_to_monday_half_open():
    out = calendar_anchors(THURSDAY)
    assert _line(out, "- this week") == "- this week [2026-09-07, 2026-09-14)"
    assert _line(out, "- last week") == "- last week [2026-08-31, 2026-09-07)"


def test_day_anchors():
    out = calendar_anchors(THURSDAY)
    assert _line(out, "- today") == "- today 2026-09-10, tomorrow 2026-09-11, yesterday 2026-09-09"


def test_month_and_year_anchors():
    out = calendar_anchors(THURSDAY)
    assert _line(out, "- this month") == "- this month [2026-09-01, 2026-10-01)"
    assert _line(out, "- last month") == "- last month [2026-08-01, 2026-09-01)"
    assert _line(out, "- this year") == "- this year [2026-01-01, 2027-01-01)"
    assert _line(out, "- last year") == "- last year [2025-01-01, 2026-01-01)"


def test_on_a_monday_this_week_starts_today():
    out = calendar_anchors(datetime(2026, 9, 7, 8, 0))
    assert _line(out, "- this week") == "- this week [2026-09-07, 2026-09-14)"
    assert _line(out, "- last week") == "- last week [2026-08-31, 2026-09-07)"


def test_on_a_sunday_this_week_still_starts_on_the_previous_monday():
    out = calendar_anchors(datetime(2026, 9, 13, 23, 59))
    assert _line(out, "- this week") == "- this week [2026-09-07, 2026-09-14)"
    assert _line(out, "- last week") == "- last week [2026-08-31, 2026-09-07)"


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        # January: last month and last year roll back into the previous year.
        (
            datetime(2026, 1, 15),
            ("- this month [2026-01-01, 2026-02-01)", "- last month [2025-12-01, 2026-01-01)"),
        ),
        # December: next month rolls forward into the next year.
        (
            datetime(2026, 12, 31),
            ("- this month [2026-12-01, 2027-01-01)", "- last month [2026-11-01, 2026-12-01)"),
        ),
        # March in a leap year: the 28+4 trick must not skip February's length.
        (
            datetime(2028, 3, 1),
            ("- this month [2028-03-01, 2028-04-01)", "- last month [2028-02-01, 2028-03-01)"),
        ),
    ],
)
def test_month_rollover(now, expected):
    out = calendar_anchors(now)
    assert (_line(out, "- this month"), _line(out, "- last month")) == expected


def test_year_rollover_in_january():
    out = calendar_anchors(datetime(2026, 1, 1))
    assert _line(out, "- this year") == "- this year [2026-01-01, 2027-01-01)"
    assert _line(out, "- last year") == "- last year [2025-01-01, 2026-01-01)"


def test_last_period_is_listed_before_this_period():
    """Ordering is load-bearing: listed the other way round, the model answered
    "last week" with the current week's range (see module docstring)."""
    out = calendar_anchors(THURSDAY)
    lines = out.splitlines()
    for period in ("week", "month", "year"):
        assert lines.index(_line(out, f"- last {period}")) < lines.index(_line(out, f"- this {period}"))


def test_anchors_tell_the_model_not_to_recompute_and_keep_the_past_n_formula():
    out = calendar_anchors(THURSDAY)
    assert "use verbatim, do not recompute" in out
    assert _line(out, "- past N") == "- past N days/months: [today − N, tomorrow)"


def test_anchors_have_no_format_placeholders():
    """The block is substituted into a ``str.format`` template as a value, so a
    brace in it would be harmless there, but it is also copied into the
    model's context: keep it free of anything that looks like a placeholder."""
    out = calendar_anchors(THURSDAY)
    assert "{" not in out and "}" not in out
