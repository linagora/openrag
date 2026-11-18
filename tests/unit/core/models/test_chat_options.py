"""What a chat request asks for, read once from its metadata."""

import pytest
from core.models.chat import ChatOptions, RetrievalOverrides


def test_empty_metadata_gives_the_defaults():
    assert ChatOptions.from_metadata(None) == ChatOptions()
    assert ChatOptions.from_metadata({}) == ChatOptions()


@pytest.mark.parametrize(
    "key",
    [
        "require_retrieval",
        "bypass_query_contextualization",
        "include_retrieval_trace",
        "compare_original_query",
        "include_all_retrieved_sources",
    ],
)
def test_diagnostic_and_retrieval_flags_count_only_json_true(key):
    assert getattr(ChatOptions.from_metadata({key: True}), key) is True
    for value in ("true", 1, "yes"):
        assert getattr(ChatOptions.from_metadata({key: value}), key) is False


@pytest.mark.parametrize("key", ["websearch", "use_map_reduce", "spoken_style_answer"])
def test_source_and_answer_flags_accept_any_truthy_value(key):
    assert getattr(ChatOptions.from_metadata({key: "yes"}), key) is True
    assert getattr(ChatOptions.from_metadata({key: 0}), key) is False


def test_attachments_keep_only_well_formed_ids():
    options = ChatOptions.from_metadata(
        {"attachments": [{"id": "f1"}, {"id": ""}, {"id": 3}, "f2", {"name": "x"}, {"id": "f1"}]}
    )
    assert options.attachment_ids == ("f1", "f1")
    assert options.attachments_sent is True


def test_attachments_that_are_not_a_list_are_ignored_but_still_reported_as_sent():
    options = ChatOptions.from_metadata({"attachments": {"id": "f1"}})
    assert options.attachment_ids == ()
    assert options.attachments_sent is True


def test_retrieval_overrides_list_only_what_the_request_changes():
    overrides = RetrievalOverrides.from_metadata(
        {"retrieval_top_k": 5, "retrieval_similarity_threshold": 1, "retrieval_disable_reranker": True}
    )
    assert overrides.as_options() == {"top_k": 5, "similarity_threshold": 1.0, "disable_reranker": True}
    assert RetrievalOverrides.from_metadata({"retrieval_disable_expansion": "true"}).as_options() == {}


@pytest.mark.parametrize("threshold", ["abc", [1], {"a": 1}])
def test_a_threshold_that_is_not_a_number_is_no_override(threshold):
    """Text completions don't validate their metadata: such a value is ignored there, never a 500."""
    options = ChatOptions.from_metadata({"retrieval_similarity_threshold": threshold})
    assert options.retrieval.similarity_threshold is None
    assert options.retrieval.as_options() == {}


@pytest.mark.parametrize(
    ("metadata", "mode"),
    [
        ({}, "contextualized"),
        ({"bypass_query_contextualization": True}, "original"),
        ({"compare_original_query": True}, "compare"),
        ({"compare_original_query": True, "bypass_query_contextualization": True}, "compare"),
    ],
)
def test_query_mode(metadata, mode):
    assert ChatOptions.from_metadata(metadata).query_mode == mode
