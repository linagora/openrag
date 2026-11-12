"""What a chat request asks for, read once from its metadata."""

import pytest
from core.models.chat import ChatOptions, RagMode, RetrievalOverrides, parse_rag_mode


def test_empty_metadata_gives_the_defaults():
    assert ChatOptions.from_metadata(None) == ChatOptions()
    assert ChatOptions.from_metadata({}) == ChatOptions()
    assert ChatOptions().rag_mode is RagMode.CLASSIC


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


def test_parse_rag_mode_accepts_classic():
    assert parse_rag_mode(None) is RagMode.CLASSIC
    assert parse_rag_mode("classic") is RagMode.CLASSIC


def test_parse_rag_mode_refuses_agentic_until_it_exists():
    with pytest.raises(ValueError, match="not available yet"):
        parse_rag_mode("agentic")


@pytest.mark.parametrize("value", ["Classic", "simple", "", 1])
def test_parse_rag_mode_refuses_an_unknown_mode(value):
    with pytest.raises(ValueError, match="rag_mode must be one of: classic"):
        parse_rag_mode(value)
