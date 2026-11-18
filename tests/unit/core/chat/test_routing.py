"""Which route a chat request takes, from its model and its options."""

import pytest
from core.chat.routing import Route, is_direct_llm_model, resolve_route
from core.models.chat import ChatOptions


@pytest.mark.parametrize(
    ("model", "direct"),
    [(None, True), ("", True), ("Mistral-Small", True), ("openrag-hr", False), ("mistral-small", False)],
)
def test_is_direct_llm_model(model, direct):
    assert is_direct_llm_model(model, "Mistral-Small") is direct


@pytest.mark.parametrize(
    ("partitions", "websearch", "route"),
    [
        (None, False, Route.DIRECT),
        (None, True, Route.WEB),
        (["hr"], False, Route.DOCUMENTS),
        (["hr"], True, Route.DOCUMENTS_AND_WEB),
        ([], False, Route.DOCUMENTS),
    ],
)
def test_resolve_route(partitions, websearch, route):
    assert resolve_route(partitions, ChatOptions(websearch=websearch)) is route


def test_route_properties():
    assert not Route.DIRECT.searches
    assert all(route.searches for route in (Route.WEB, Route.DOCUMENTS, Route.DOCUMENTS_AND_WEB))
    assert [route for route in Route if route.has_documents] == [Route.DOCUMENTS, Route.DOCUMENTS_AND_WEB]
    assert [route for route in Route if route.has_web] == [Route.WEB, Route.DOCUMENTS_AND_WEB]
