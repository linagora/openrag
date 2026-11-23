"""Unit tests for ``core.tools.spec``: declaring tools and the registry they are offered from."""

from __future__ import annotations

import json
from typing import Annotated, Literal

import pytest
from core.tools import DEFAULT_TOOL_TIMEOUT, ToolContext, ToolRegistry, tool, tool_spec
from pydantic import Field


class Library:
    @tool("search", "Search the documents")
    async def search(
        self,
        ctx: ToolContext,
        queries: Annotated[list[str], Field(min_length=1, max_length=3, description="What to look for")],
        order: Literal["newest", "oldest"] | None = None,
    ) -> str:
        return "found"

    @tool("read", "Read a file", timeout=90)
    async def read(self, ctx: ToolContext, file_id: str, title: str = "") -> str:
        return "text"

    async def helper(self) -> None:
        """Not a tool."""


class MoreLibrary(Library):
    @tool("delete", "Delete a file", read_only=False)
    async def delete(self, ctx: ToolContext, file_id: str) -> str:
        return "deleted"


def test_the_registry_holds_every_tool_method_in_declaration_order():
    registry = ToolRegistry.from_toolsets(MoreLibrary())

    assert registry.names == ["search", "read", "delete"]
    assert registry.get("read").timeout == 90
    assert registry.get("search").timeout == DEFAULT_TOOL_TIMEOUT
    assert registry.get("delete").read_only is False
    assert registry.get("helper") is None


def test_the_schema_is_what_the_model_fills_in_without_the_context():
    schema = tool_spec(Library().search).schema()

    assert schema["type"] == "function"
    function = schema["function"]
    assert function["name"] == "search"
    assert function["description"] == "Search the documents"
    parameters = function["parameters"]
    assert set(parameters["properties"]) == {"queries", "order"}
    assert parameters["required"] == ["queries"]
    assert parameters["properties"]["queries"]["description"] == "What to look for"
    assert parameters["properties"]["queries"]["maxItems"] == 3
    assert parameters["additionalProperties"] is False


def test_an_optional_parameter_is_shown_as_its_type_and_a_null_is_still_accepted():
    spec = tool_spec(Library().search)

    order = spec.schema()["function"]["parameters"]["properties"]["order"]

    assert order == {"enum": ["newest", "oldest"], "type": "string"}, "no anyOf with null, no default: null"
    assert spec.params.model_validate({"queries": ["x"], "order": None}).order is None
    assert tool_spec(Library().read).schema()["function"]["parameters"]["properties"]["title"]["default"] == "", (
        "a default other than null is kept"
    )


def test_the_schema_drops_titles_but_keeps_a_property_called_title():
    parameters = tool_spec(Library().read).schema()["function"]["parameters"]

    assert "title" not in parameters
    assert set(parameters["properties"]) == {"file_id", "title"}
    assert all("title" not in prop for prop in parameters["properties"].values())


def test_two_tools_with_one_name_are_refused():
    with pytest.raises(ValueError, match="Two tools are named 'search'"):
        ToolRegistry.from_toolsets(Library(), Library())


def test_a_subset_keeps_the_order_asked_and_refuses_unknown_names():
    registry = ToolRegistry.from_toolsets(MoreLibrary())

    assert registry.subset(["delete", "search"]).names == ["delete", "search"]
    with pytest.raises(KeyError, match="nope"):
        registry.subset(["search", "nope"])


def test_a_tool_must_be_async():
    with pytest.raises(TypeError, match="async"):

        @tool("sync", "Not async")
        def sync(self, ctx):  # pragma: no cover - never called
            return None


def test_every_parameter_needs_a_type_hint():
    class Untyped:
        @tool("untyped", "No hint")
        async def untyped(self, ctx: ToolContext, query) -> str:
            return query

    with pytest.raises(TypeError, match="'query' needs a type hint"):
        ToolRegistry.from_toolsets(Untyped())


def test_the_model_is_shown_the_tool_s_description_never_its_docstring():
    class Documented:
        @tool("lookup", "Look a word up")
        async def lookup(self, ctx: ToolContext, word: str) -> str:
            """Internal notes for developers, not for the model."""
            return word

    schema = json.dumps(tool_spec(Documented().lookup).schema())

    assert '"description": "Look a word up"' in schema
    assert "Internal notes" not in schema


async def test_the_decorator_leaves_the_method_as_it_is():
    library = Library()

    assert await library.search(None, ["x"]) == "found", "still a plain async method"
    assert tool_spec(library.search).handler == library.search


def test_a_method_without_the_decorator_has_no_spec():
    with pytest.raises(TypeError, match="is not a @tool method"):
        tool_spec(Library().helper)


def test_a_tool_must_take_the_context():
    class Contextless:
        @tool("bare", "Takes nothing")
        async def bare(self) -> str:
            return ""

    with pytest.raises(TypeError, match="must take the ToolContext as its first parameter"):
        ToolRegistry.from_toolsets(Contextless())


@pytest.mark.parametrize("variadic", ["args", "kwargs"])
def test_a_tool_takes_named_parameters_only(variadic):
    class Variadic:
        if variadic == "args":

            @tool("many", "Takes any")
            async def many(self, ctx: ToolContext, *words: str) -> str:
                return ""
        else:

            @tool("many", "Takes any")
            async def many(self, ctx: ToolContext, **words: str) -> str:
                return ""

    with pytest.raises(TypeError, match="parameter 'words' must be a named parameter"):
        ToolRegistry.from_toolsets(Variadic())


def test_a_keyword_only_parameter_is_one_the_model_fills_in():
    class KeywordOnly:
        @tool("find", "Find a word")
        async def find(self, ctx: ToolContext, *, word: str, limit: int = 5) -> str:
            return word

    parameters = tool_spec(KeywordOnly().find).schema()["function"]["parameters"]

    assert set(parameters["properties"]) == {"word", "limit"}
    assert parameters["required"] == ["word"]
