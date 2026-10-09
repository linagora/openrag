"""Unit tests for :class:`core.models.query.Query.to_milvus_filter`."""

from __future__ import annotations

import pytest
from core.models.query import Query, SearchQueries, TemporalPredicate
from pydantic import ValidationError


def _filter(*values: tuple[str, str]) -> str | None:
    return Query(
        query="q",
        temporal_filters=[TemporalPredicate(operator=op, value=value) for op, value in values],
    ).to_milvus_filter()


def test_no_predicates_means_no_filter():
    assert Query(query="q").to_milvus_filter() is None
    assert Query(query="q", temporal_filters=[]).to_milvus_filter() is None


def test_aware_utc_values_render_unchanged_and_and_combined():
    assert _filter((">=", "2026-09-07T00:00:00+00:00"), ("<", "2026-09-14T00:00:00+00:00")) == (
        'created_at >= ISO "2026-09-07T00:00:00+00:00" and created_at < ISO "2026-09-14T00:00:00+00:00"'
    )


def test_a_bare_date_is_read_as_midnight_utc():
    """The calendar anchors are bare dates the model is told to copy verbatim;
    a verbatim copy must filter, not be dropped."""
    assert _filter((">=", "2026-09-07")) == 'created_at >= ISO "2026-09-07T00:00:00+00:00"'


def test_a_naive_datetime_is_read_as_utc():
    assert _filter(("<", "2026-09-14T00:00:00")) == 'created_at < ISO "2026-09-14T00:00:00+00:00"'


def test_a_zulu_suffix_is_normalised_to_an_explicit_offset():
    assert _filter((">=", "2026-09-07T00:00:00Z")) == 'created_at >= ISO "2026-09-07T00:00:00+00:00"'


def test_a_non_utc_offset_is_kept_as_given():
    assert _filter((">=", "2026-09-07T02:00:00+02:00")) == 'created_at >= ISO "2026-09-07T02:00:00+02:00"'


def test_search_queries_rejects_overflow_instead_of_truncating():
    queries = [Query(query=f"aspect {i}") for i in range(8)]
    assert len(SearchQueries(query_list=queries).query_list) == 8

    with pytest.raises(ValidationError, match="at most 8 items"):
        SearchQueries(query_list=[*queries, Query(query="aspect 8")])


def test_an_unparseable_value_is_dropped_and_the_rest_kept():
    assert _filter((">=", "last monday"), ("<", "2026-09-14")) == 'created_at < ISO "2026-09-14T00:00:00+00:00"'
    assert _filter((">=", "last monday")) is None
