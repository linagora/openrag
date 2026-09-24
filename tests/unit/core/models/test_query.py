"""Unit tests for :class:`core.models.query.Query.to_milvus_filter`."""

from __future__ import annotations

from core.models.query import Query, TemporalPredicate


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


def test_an_unparseable_value_is_dropped_and_the_rest_kept():
    assert _filter((">=", "last monday"), ("<", "2026-09-14")) == 'created_at < ISO "2026-09-14T00:00:00+00:00"'
    assert _filter((">=", "last monday")) is None


# --------------------------------------------------------------------------- #
# date_order — "the last / first X" is an ordering, not a period
# --------------------------------------------------------------------------- #


def test_date_order_defaults_to_none_and_accepts_both_directions():
    assert Query(query="q").date_order is None
    assert Query(query="q", date_order="newest").date_order == "newest"
    assert Query(query="q", date_order="oldest").date_order == "oldest"


def test_date_order_rejects_other_values():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Query(query="q", date_order="latest")


def test_date_order_does_not_leak_into_the_milvus_filter():
    """Ordering is applied on the ranked list, never as a Milvus predicate."""
    assert Query(query="q", date_order="newest").to_milvus_filter() is None


def test_date_order_parses_from_the_contextualizer_json_and_may_be_omitted():
    from core.models.query import SearchQueries

    parsed = SearchQueries.model_validate_json(
        '{"requires_retrieval": true, "query_list": [{"query": "meeting notes", "temporal_filters": null, '
        '"date_order": "newest"}, {"query": "old shape", "temporal_filters": null}]}'
    )
    assert [q.date_order for q in parsed.query_list] == ["newest", None]


def test_search_queries_date_order_is_the_first_sub_query_that_asks_for_one():
    from core.models.query import SearchQueries

    assert SearchQueries(query_list=[Query(query="a"), Query(query="b")]).date_order is None
    assert SearchQueries(query_list=[Query(query="a"), Query(query="b", date_order="oldest")]).date_order == "oldest"
    assert (
        SearchQueries(
            query_list=[Query(query="a", date_order="newest"), Query(query="b", date_order="oldest")]
        ).date_order
        == "newest"
    )


def test_str_mentions_the_order_only_when_set():
    assert "Order" not in str(Query(query="q"))
    assert str(Query(query="q", date_order="newest")).endswith("Order: newest")
