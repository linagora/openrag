"""Unit tests for :mod:`core.prompts.calendar_anchors`."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone

import pytest
from core.models.query import Query, TemporalPredicate
from core.prompts.calendar_anchors import RECENT_DAYS, calendar_anchors

# Thursday, in the middle of a week / month / year — the case that surfaced
# the bug (Mistral resolved "last week" to the past 7 days on this date).
THURSDAY = datetime(2026, 9, 10, 10, 23, 21, tzinfo=UTC)

TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\S+?(?=[,)\s])")


def _line(anchors: str, prefix: str) -> str:
    for line in anchors.splitlines():
        if line.startswith(prefix):
            return line
    raise AssertionError(f"no line starting with {prefix!r} in:\n{anchors}")


def _range(anchors: str, prefix: str) -> tuple[str, str]:
    """The ``[start, end)`` boundaries of one anchor line."""
    line = _line(anchors, prefix)
    m = re.search(r"\[(\S+), (\S+)\)$", line)
    assert m, line
    return m.group(1), m.group(2)


def _ts(day: str) -> str:
    return f"{day}T00:00:00+00:00"


DATE = re.compile(r"(?<![\dT:+-])\d{4}-\d{2}-\d{2}(?![\dT])")


def test_week_anchors_are_monday_to_monday_half_open():
    out = calendar_anchors(THURSDAY)
    assert _range(out, "- this week") == ("2026-09-07", "2026-09-14")
    assert _range(out, "- last week") == ("2026-08-31", "2026-09-07")


def test_day_anchors():
    out = calendar_anchors(THURSDAY)
    assert _line(out, "- today") == (
        f"- today {_ts('2026-09-10')}, tomorrow {_ts('2026-09-11')}, yesterday {_ts('2026-09-09')}"
    )


def test_month_and_year_anchors():
    out = calendar_anchors(THURSDAY)
    assert _range(out, "- this month") == ("2026-09-01", "2026-10-01")
    assert _range(out, "- last month") == ("2026-08-01", "2026-09-01")
    assert _range(out, "- this year") == ("2026-01-01", "2027-01-01")
    assert _range(out, "- last year") == ("2025-01-01", "2026-01-01")


def test_recent_anchor_is_the_past_90_days_up_to_tomorrow():
    """The prompt promises "recent / latest → past 90 days" but had no concrete
    range for it, so the model rolled its own (three calendar months, ending
    today). The anchor names both words and closes on tomorrow so a document
    created today is included."""
    out = calendar_anchors(THURSDAY)
    assert RECENT_DAYS == 90
    assert _line(out, "- recent / latest (past 90 days)")
    assert _range(out, "- recent / latest") == ("2026-06-12", "2026-09-11")


def test_past_n_formula_names_every_unit_the_prompt_accepts():
    """ "past N weeks" / "past N years" are promised by the template; the
    formula must not read as days and months only."""
    out = calendar_anchors(THURSDAY)
    assert _line(out, "- past N") == "- past N days/weeks/months/years: [today − N, tomorrow)"


def test_past_two_weeks_is_given_as_a_worked_example_of_the_formula():
    """A rolling window the model kept getting wrong (17 days) from the formula
    alone. Labelled "e.g." on purpose: as a plain anchor, "past 3 weeks" copied
    this range instead of applying the formula."""
    out = calendar_anchors(THURSDAY)
    assert _range(out, "- e.g. past 2 weeks") == ("2026-08-27", "2026-09-11")
    lines = out.splitlines()
    assert lines.index(_line(out, "- e.g. past 2 weeks")) == len(lines) - 2


def test_on_a_monday_this_week_starts_today():
    out = calendar_anchors(datetime(2026, 9, 7, 8, 0, tzinfo=UTC))
    assert _range(out, "- this week") == ("2026-09-07", "2026-09-14")
    assert _range(out, "- last week") == ("2026-08-31", "2026-09-07")


def test_on_a_sunday_this_week_still_starts_on_the_previous_monday():
    out = calendar_anchors(datetime(2026, 9, 13, 23, 59, tzinfo=UTC))
    assert _range(out, "- this week") == ("2026-09-07", "2026-09-14")
    assert _range(out, "- last week") == ("2026-08-31", "2026-09-07")


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        # January: last month and last year roll back into the previous year.
        (
            datetime(2026, 1, 15, tzinfo=UTC),
            (("2026-01-01", "2026-02-01"), ("2025-12-01", "2026-01-01")),
        ),
        # December: next month rolls forward into the next year.
        (
            datetime(2026, 12, 31, tzinfo=UTC),
            (("2026-12-01", "2027-01-01"), ("2026-11-01", "2026-12-01")),
        ),
        # March in a leap year: the 28+4 trick must not skip February's length.
        (
            datetime(2028, 3, 1, tzinfo=UTC),
            (("2028-03-01", "2028-04-01"), ("2028-02-01", "2028-03-01")),
        ),
    ],
)
def test_month_rollover(now, expected):
    out = calendar_anchors(now)
    assert (_range(out, "- this month"), _range(out, "- last month")) == expected


def test_year_rollover_in_january():
    out = calendar_anchors(datetime(2026, 1, 1, tzinfo=UTC))
    assert _range(out, "- this year") == ("2026-01-01", "2027-01-01")
    assert _range(out, "- last year") == ("2025-01-01", "2026-01-01")


def test_now_is_read_in_utc_not_in_its_own_zone():
    """01:30 in Paris on the 17th is still 23:30 UTC on the 16th. Document
    created_at timestamps are UTC, so "today" must be the UTC day."""
    paris = timezone(timedelta(hours=2))
    out = calendar_anchors(datetime(2026, 9, 17, 1, 30, tzinfo=paris))
    assert _line(out, "- today").startswith(f"- today {_ts('2026-09-16')}, ")
    assert calendar_anchors(datetime(2026, 9, 17, 1, 30, tzinfo=paris)) == calendar_anchors(
        datetime(2026, 9, 16, 23, 30, tzinfo=UTC)
    )


def test_a_naive_now_is_taken_as_utc():
    out = calendar_anchors(datetime(2026, 9, 16, 23, 59))
    assert _line(out, "- today").startswith(f"- today {_ts('2026-09-16')}, ")


def test_only_the_today_line_spells_out_full_timestamps():
    """One line in the full ``T00:00:00+00:00`` form shows the format to
    reproduce; every other boundary is a bare date, because the suffix costs
    as many tokens as the date itself and the block is copied into a prompt
    that competes for a small context window."""
    out = calendar_anchors(THURSDAY)
    timestamps = TIMESTAMP.findall(out)
    assert len(timestamps) == 3
    assert all(t.endswith("T00:00:00+00:00") for t in timestamps)
    assert all(line.startswith("- today") for line in out.splitlines() if "T00:00:00" in line)
    assert len(DATE.findall(out)) == 2 * 8  # eight [start, end) ranges


def test_every_boundary_survives_the_milvus_filter_whether_copied_bare_or_completed():
    """The model copies the anchors verbatim into ``temporal_filters``. A bare
    date, or one it completed to midnight UTC, must both reach Milvus as the
    same aware timestamp rather than being dropped (which would silently turn
    a dated question into an unfiltered search)."""
    out = calendar_anchors(THURSDAY)
    for day in DATE.findall(out):
        for value in (day, f"{day}T00:00:00+00:00"):
            query = Query(query="q", temporal_filters=[TemporalPredicate(operator=">=", value=value)])
            assert query.to_milvus_filter() == f'created_at >= ISO "{day}T00:00:00+00:00"', value
    for value in TIMESTAMP.findall(out):
        parsed = datetime.fromisoformat(value)
        assert parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0), value


def test_last_period_is_listed_before_this_period():
    """Ordering is load-bearing: listed the other way round, the model answered
    "last week" with the current week's range (see module docstring)."""
    out = calendar_anchors(THURSDAY)
    lines = out.splitlines()
    for period in ("week", "month", "year"):
        assert lines.index(_line(out, f"- last {period}")) < lines.index(_line(out, f"- this {period}"))


def test_anchors_tell_the_model_not_to_recompute_and_end_with_the_past_n_formula():
    out = calendar_anchors(THURSDAY)
    assert "use verbatim, do not recompute" in out
    assert out.splitlines()[-1].startswith("- past N ")


def test_anchors_have_no_format_placeholders():
    """The block is substituted into a ``str.format`` template as a value, so a
    brace in it would be harmless there, but it is also copied into the
    model's context: keep it free of anything that looks like a placeholder."""
    out = calendar_anchors(THURSDAY)
    assert "{" not in out and "}" not in out
