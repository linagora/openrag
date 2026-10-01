"""Pre-computed calendar anchors for the query contextualizer.

The contextualizer prompt asks the LLM to turn "last week" / "this month" /
"yesterday" into half-open ``created_at`` ranges. Left to do the calendar
arithmetic itself, a mid-size model gets the week boundaries wrong: on a
Thursday, Mistral Small resolved "last week" to the past seven days and
"this week" to a window starting the day before, which matched nothing and
fell through to the filterless fallback. Handing it the boundaries already
computed removes the arithmetic from the model entirely.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

# "recent" / "latest" resolve to this many days, matching the prompt's
# "~3 months, NOT a year" rule and its "Latest safety bulletins" example.
RECENT_DAYS = 90


def _first_of_next_month(day: date) -> date:
    return (day.replace(day=28) + timedelta(days=4)).replace(day=1)


def _first_of_previous_month(day: date) -> date:
    return (day.replace(day=1) - timedelta(days=1)).replace(day=1)


def _utc_midnight(day: date) -> str:
    """``day`` at 00:00 UTC as the timezone-aware ISO 8601 the filter expects."""
    return datetime.combine(day, time.min, tzinfo=UTC).isoformat()


def calendar_anchors(now: datetime) -> str:
    """Half-open date ranges for the relative periods the prompt resolves.

    ``now`` is read in UTC (an aware value is converted, a naive one is taken
    as already UTC) because document ``created_at`` timestamps are UTC: on a
    host running in another zone, the local date near midnight is not the UTC
    date the filter is compared against.

    Weeks start on Monday, matching the prompt's "Week starts Monday" rule.
    Every range is ``[start, end)`` so it can be copied verbatim into two
    ``>=`` / ``<`` predicates. Only the today line is spelled out as full
    ``T00:00:00+00:00`` timestamps, as the format to reproduce; the other
    boundaries are bare dates, which halves the block's token cost (a suffix
    costs as much as the date itself). ``Query.to_milvus_filter`` reads a
    value without a zone as UTC midnight, so a boundary copied bare still
    filters.

    The block is kept terse because the whole contextualizer prompt competes
    for a small context window, but its shape was chosen by replaying the
    prompt against the deployment's LLM, not by taste: one anchor per line
    with "last X" listed before "this X" (two per line, or "this" first, made
    the model answer "last week" with the current week's range), and the
    closing formula line (without it the model returned no filter at all for
    an explicit month such as "January 2024", as if only listed periods were
    filterable). Dropping the year anchors cost accuracy on the English "last
    week"; wording the formula as "never < today" broke "last month". Periods
    the prompt promises but that had no concrete anchor ("recent", "past N
    weeks") came back with a rolling window of the wrong length.
    """
    today = (now.astimezone(UTC) if now.tzinfo is not None else now).date()
    tomorrow = today + timedelta(days=1)
    yesterday = today - timedelta(days=1)
    this_monday = today - timedelta(days=today.weekday())
    next_monday = this_monday + timedelta(days=7)
    last_monday = this_monday - timedelta(days=7)
    this_month = today.replace(day=1)
    next_month = _first_of_next_month(today)
    last_month = _first_of_previous_month(today)
    this_year = today.replace(month=1, day=1)
    next_year = this_year.replace(year=this_year.year + 1)
    last_year = this_year.replace(year=this_year.year - 1)
    recent_start = today - timedelta(days=RECENT_DAYS)
    two_weeks_ago = today - timedelta(days=14)
    ts = _utc_midnight
    return (
        "Calendar anchors (use verbatim, do not recompute):\n"
        f"- today {ts(today)}, tomorrow {ts(tomorrow)}, yesterday {ts(yesterday)}\n"
        f"- last week [{last_monday}, {this_monday})\n"
        f"- this week [{this_monday}, {next_monday})\n"
        f"- last month [{last_month}, {this_month})\n"
        f"- this month [{this_month}, {next_month})\n"
        f"- last year [{last_year}, {this_year})\n"
        f"- this year [{this_year}, {next_year})\n"
        f"- recent / latest (past {RECENT_DAYS} days) [{recent_start}, {tomorrow})\n"
        # One worked instance of the formula: without it the model resolved
        # "past 2 weeks" to a 17-day window in both languages. Labelled as an
        # example, otherwise "past 3 weeks" copies this range instead of
        # applying the formula. Kept above the formula line, which must stay
        # last.
        f"- e.g. past 2 weeks [{two_weeks_ago}, {tomorrow})\n"
        "- past N days/weeks/months/years: [today − N, tomorrow)"
    )
