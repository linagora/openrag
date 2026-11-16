"""What a tool is, how one is declared, and the registry the agent's tools are offered from.

A tool is an async method marked with ``@tool``. Its first parameter after
``self`` is the request's ``ToolContext``; the others are what the model fills
in, and their type hints (``Annotated[..., Field(description=...)]`` for a
description or a bound) are the JSON schema the model is shown. Tools are
methods of objects that hold their services, so ``di`` builds those objects
and the registry is made from them, not filled at import time.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any, get_type_hints

from core.utils.exceptions import OpenRAGError
from pydantic import BaseModel, ConfigDict, create_model

#: Seconds a tool call may take unless its ``@tool`` says otherwise.
DEFAULT_TOOL_TIMEOUT = 30.0

_TOOL_MARK = "__openrag_tool__"


class ToolError(OpenRAGError):
    """A failure the model is told about, in words it can act on ("no file 'x' in this scope").

    Raised by a tool; ``run_tool`` turns it into the tool's answer. Never reaches
    the HTTP layer.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, code="TOOL_ERROR", status_code=400)


@dataclass(frozen=True)
class _ToolMark:
    name: str
    description: str
    read_only: bool
    timeout: float


@dataclass(frozen=True)
class ToolSpec:
    """One tool: what the model is shown about it, and the call that runs it.

    *handler* is called as ``handler(ctx, **arguments)`` with the arguments
    validated against *params*.
    """

    name: str
    description: str
    params: type[BaseModel]
    handler: Callable[..., Awaitable[Any]]
    read_only: bool = True
    timeout: float = DEFAULT_TOOL_TIMEOUT

    def schema(self) -> dict[str, Any]:
        """The tool as an OpenAI ``tools`` entry."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": _optional_shown_plainly(_without_titles(self.params.model_json_schema())),
            },
        }


def tool(
    name: str,
    description: str,
    *,
    read_only: bool = True,
    timeout: float = DEFAULT_TOOL_TIMEOUT,
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """Mark an async method as a tool named *name*, described to the model by *description*.

    The method's hints are read when the registry is built, so the types they
    name (``ToolContext`` included) must be importable at run time, not only
    for type checkers.
    """

    def mark(method: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        if not inspect.iscoroutinefunction(method):
            raise TypeError(f"Tool '{name}' must be an async method")
        setattr(method, _TOOL_MARK, _ToolMark(name, description, read_only, timeout))
        return method

    return mark


def tool_spec(method: Callable[..., Awaitable[Any]]) -> ToolSpec:
    """The ``ToolSpec`` of a bound ``@tool`` method."""
    mark: _ToolMark | None = getattr(method, _TOOL_MARK, None)
    if mark is None:
        raise TypeError(f"{method!r} is not a @tool method")
    return ToolSpec(
        name=mark.name,
        description=mark.description,
        params=_params_model(method, mark.name),
        handler=method,
        read_only=mark.read_only,
        timeout=mark.timeout,
    )


def _params_model(method: Callable[..., Any], name: str) -> type[BaseModel]:
    """A model of the parameters the model fills in: every one after the context."""
    hints = get_type_hints(method, include_extras=True)
    parameters = list(inspect.signature(method).parameters.values())
    if not parameters:
        raise TypeError(f"Tool '{name}' must take the ToolContext as its first parameter")
    fields: dict[str, Any] = {}
    for parameter in parameters[1:]:
        if parameter.kind not in (parameter.POSITIONAL_OR_KEYWORD, parameter.KEYWORD_ONLY):
            raise TypeError(f"Tool '{name}': parameter '{parameter.name}' must be a named parameter")
        if parameter.name not in hints:
            raise TypeError(f"Tool '{name}': parameter '{parameter.name}' needs a type hint")
        default = ... if parameter.default is parameter.empty else parameter.default
        fields[parameter.name] = (hints[parameter.name], default)
    model_name = "".join(part.capitalize() for part in name.split("_")) + "Arguments"
    # Unknown arguments are an error the model reads, not something silently dropped.
    return create_model(model_name, __config__=ConfigDict(extra="forbid"), **fields)


def _without_titles(schema: Any, *, in_properties: bool = False) -> Any:
    """*schema* without pydantic's ``title`` keys, which the model doesn't need.

    A property that is itself called ``title`` is kept.
    """
    if isinstance(schema, list):
        return [_without_titles(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    return {
        key: _without_titles(value, in_properties=key == "properties" and not in_properties)
        for key, value in schema.items()
        if in_properties or key != "title"
    }


_NULL = {"type": "null"}


def _optional_shown_plainly(parameters: dict[str, Any]) -> dict[str, Any]:
    """*parameters* with each ``X | None = None`` parameter shown as ``X``.

    Pydantic shows one as ``anyOf: [X, {"type": "null"}]`` with ``default:
    null``, which the model is sent with every reply. Leaving it out of
    ``required`` already says it is optional; a null the model sends anyway
    is still accepted, as the arguments are validated by the full model.
    """
    properties = {}
    for name, prop in parameters.get("properties", {}).items():
        branches = prop.get("anyOf")
        if "default" in prop and prop["default"] is None and isinstance(branches, list) and _NULL in branches:
            others = [branch for branch in branches if branch != _NULL]
            if len(others) == 1:
                prop = {**others[0], **{key: value for key, value in prop.items() if key not in ("anyOf", "default")}}
        properties[name] = prop
    return {**parameters, "properties": properties} if "properties" in parameters else parameters


class ToolRegistry:
    """The tools an agent can be offered, by name."""

    def __init__(self, specs: Iterable[ToolSpec]) -> None:
        self._specs: dict[str, ToolSpec] = {}
        for spec in specs:
            if spec.name in self._specs:
                raise ValueError(f"Two tools are named '{spec.name}'")
            self._specs[spec.name] = spec

    @classmethod
    def from_toolsets(cls, *toolsets: object) -> ToolRegistry:
        """Every ``@tool`` method of *toolsets*, in the order they are declared, base classes first."""
        specs = []
        for toolset in toolsets:
            names = dict.fromkeys(
                name
                for klass in reversed(type(toolset).__mro__)
                for name, attribute in vars(klass).items()
                if callable(attribute) and hasattr(attribute, _TOOL_MARK)
            )
            specs.extend(tool_spec(getattr(toolset, name)) for name in names)
        return cls(specs)

    @property
    def names(self) -> list[str]:
        return list(self._specs)

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def subset(self, names: Iterable[str]) -> ToolRegistry:
        """The tools named *names*, in that order: the ones an agent profile offers.

        An unknown name is a configuration error, raised here rather than
        leaving the agent without a tool it is told about.
        """
        names = list(names)
        unknown = [name for name in names if name not in self._specs]
        if unknown:
            raise KeyError(f"Unknown tools: {', '.join(unknown)}")
        return ToolRegistry(self._specs[name] for name in names)

    def schemas(self) -> list[dict[str, Any]]:
        """Every tool as an OpenAI ``tools`` entry."""
        return [spec.schema() for spec in self._specs.values()]

    def __len__(self) -> int:
        return len(self._specs)


__all__ = ["DEFAULT_TOOL_TIMEOUT", "ToolError", "ToolRegistry", "ToolSpec", "tool", "tool_spec"]
