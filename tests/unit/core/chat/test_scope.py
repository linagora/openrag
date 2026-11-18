"""The scope a chat request searches."""

from core.chat.scope import Scope, within_workspace
from core.retrieval.trace import TRACE_FILE_SCOPE_KIND_KEY


def test_whole_partitions_need_no_filter():
    assert Scope(["p"]).filter_params(traced=True) is None


def test_an_empty_file_list_is_a_filter_that_matches_nothing():
    assert Scope(["p"], [], "workspace").filter_params(traced=False) == {"file_id": []}


def test_the_kind_is_added_for_the_trace_only():
    scope = Scope(["p"], ["a"], "workspace")
    assert scope.filter_params(traced=False) == {"file_id": ["a"]}
    assert scope.filter_params(traced=True) == {"file_id": ["a"], TRACE_FILE_SCOPE_KIND_KEY: "workspace"}


def test_attachments_outside_the_workspace_are_dropped_in_order_once():
    assert within_workspace(["b", "x", "a", "b"], ["a", "b"]) == ["b", "a"]
