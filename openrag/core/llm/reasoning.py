"""How much a thinking model reasons before it answers: OpenAI's ``reasoning_effort``.

One field, with OpenAI's values, wherever it is set: a chat request, an agent
profile, an LLM endpoint's default. It goes to the model as the top-level
``reasoning_effort`` of the chat request, which OpenAI, vLLM, Ollama's
``/v1``, OpenRouter, Mistral's API and Moonshot all read. Their accepted
values differ: vLLM took only ``low``/``medium``/``high`` before 0.18 and
added ``minimal`` and ``xhigh`` in 0.22, Mistral's API takes ``none`` and
``high``. An endpoint's ``reasoning_effort_map`` rewrites a level to what its
backend takes, or to ``null`` to send none.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, get_args

ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh"]

#: OpenAI's levels, from no reasoning to the most.
REASONING_EFFORTS: tuple[str, ...] = get_args(ReasoningEffort)

#: The request field, and the endpoint ``extra`` key of its default.
REASONING_EFFORT_KEY = "reasoning_effort"

#: The endpoint ``extra`` key of the level each OpenAI level is sent as.
REASONING_EFFORT_MAP_KEY = "reasoning_effort_map"


def validate_reasoning_extra(extra: dict[str, Any] | None) -> dict[str, Any] | None:
    """Reject an LLM endpoint's reasoning settings that no request could use.

    ``reasoning_effort`` is one of OpenAI's levels, or ``null`` for none. The
    map's keys are OpenAI's levels; its values are what the backend takes (any
    name, ``max`` included) or ``null`` to send no level. Raises ``ValueError``
    for a pydantic validator to turn into a 422.
    """
    if not extra:
        return extra
    effort = extra.get(REASONING_EFFORT_KEY)
    if effort is not None and effort not in REASONING_EFFORTS:
        raise ValueError(f"extra.{REASONING_EFFORT_KEY} must be one of {', '.join(REASONING_EFFORTS)}, or null")
    effort_map = extra.get(REASONING_EFFORT_MAP_KEY)
    if effort_map is None:
        return extra
    if not isinstance(effort_map, dict):
        raise ValueError(f"extra.{REASONING_EFFORT_MAP_KEY} must be an object")
    for level, sent_as in effort_map.items():
        if level not in REASONING_EFFORTS:
            raise ValueError(
                f"extra.{REASONING_EFFORT_MAP_KEY} keys must be among {', '.join(REASONING_EFFORTS)}; got {level!r}"
            )
        if sent_as is not None and (not isinstance(sent_as, str) or not sent_as.strip()):
            raise ValueError(f"extra.{REASONING_EFFORT_MAP_KEY}.{level} must be a non-empty string, or null")
    return extra


def resolve_reasoning_effort(
    payload: dict[str, Any],
    default: str | None,
    effort_map: Mapping[str, str | None] | None,
) -> None:
    """Set *payload*'s ``reasoning_effort`` as the backend gets it, in place.

    The request's level wins; without one, the endpoint's *default*. The
    endpoint's *effort_map* then rewrites it; a level mapped to ``None`` is
    not sent, nor is a ``None`` level.
    """
    effort = payload.get(REASONING_EFFORT_KEY)
    if effort is None:
        effort = default
    if effort is not None and effort_map and effort in effort_map:
        effort = effort_map[effort]
    if effort is None:
        payload.pop(REASONING_EFFORT_KEY, None)
    else:
        payload[REASONING_EFFORT_KEY] = effort


__all__ = [
    "REASONING_EFFORTS",
    "REASONING_EFFORT_KEY",
    "REASONING_EFFORT_MAP_KEY",
    "ReasoningEffort",
    "resolve_reasoning_effort",
    "validate_reasoning_extra",
]
