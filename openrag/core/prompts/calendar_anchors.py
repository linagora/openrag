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

from datetime import date, datetime, timedelta


def _first_of_next_month(day: date) -> date:
    return (day.replace(day=28) + timedelta(days=4)).replace(day=1)


def _first_of_previous_month(day: date) -> date:
    return (day.replace(day=1) - timedelta(days=1)).replace(day=1)


def calendar_anchors(now: datetime) -> str:
    """Half-open date ranges for the relative periods the prompt resolves.

    Weeks start on Monday, matching the prompt's "Week starts Monday" rule.
    Every range is ``[start, end)`` so it can be copied verbatim into two
    ``>=`` / ``<`` predicates.

    The block is kept terse (about 170 tokens) because the whole
    contextualizer prompt competes for a small context window, but its shape
    was chosen by replaying the prompt against the staging LLM, not by taste:
    one anchor per line with "last X" listed before "this X" (two per line, or
    "this" first, made the model answer "last week" with the current week's
    range), and the closing formula line (without it the model returned no
    filter at all for an explicit month such as "January 2024", as if only
    listed periods were filterable). Dropping the year anchors cost accuracy
    on the English "last week"; wording the formula as "never < today" broke
    "last month".
    """
    today = now.date()
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
    return (
        "Calendar anchors (use verbatim, do not recompute):\n"
        f"- today {today}, tomorrow {tomorrow}, yesterday {yesterday}\n"
        f"- last week [{last_monday}, {this_monday})\n"
        f"- this week [{this_monday}, {next_monday})\n"
        f"- last month [{last_month}, {this_month})\n"
        f"- this month [{this_month}, {next_month})\n"
        f"- last year [{last_year}, {this_year})\n"
        f"- this year [{this_year}, {next_year})\n"
        "- past N days/months: [today − N, tomorrow)"
    )
