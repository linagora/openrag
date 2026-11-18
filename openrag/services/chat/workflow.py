"""What ``ChatService`` asks of a chat workflow, whatever finds the sources.

A workflow gets a ``ChatRequest`` and returns the ``PreparedAnswer``
``AnswerService`` answers from. ``ChatService`` resolves the answering LLM,
picks the workflow and hands the result to ``AnswerService``; it knows nothing
of how the workflow finds the sources. The ``classic`` graph
(``services.chat.classic.ClassicWorkflow``) is the one workflow today.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from core.llm.llm import LLM
    from core.models.chat import ChatOptions
    from services.orchestrators.answer_service import PreparedAnswer

#: Builds the client's source entries from the documents and the web results
#: the answer is given (``api.routers.user.chat``: request-bound links).
PrepareSources = Callable[[list, list], list]


@dataclass(frozen=True)
class ChatRequest:
    """A chat request as a workflow gets it.

    *partitions* is the request's scope as the router resolved it (``None``
    for none); *options* its ``metadata`` flags, read once; *llm* the model
    that answers. *max_prompt_tokens* is what that model's window leaves for
    the prompt, ``None`` when the window is unknown. *started* is when the
    request reached the service (``time.perf_counter``), for the trace's total.
    """

    partitions: list[str] | None
    payload: dict
    options: ChatOptions
    llm: LLM
    prepare_sources: PrepareSources
    started: float
    request_id: str | None = None
    max_prompt_tokens: int | None = None


class ChatWorkflow(Protocol):
    """Finds a chat request's sources and prepares what ``AnswerService`` answers from."""

    async def prepare(self, request: ChatRequest) -> PreparedAnswer: ...


__all__ = ["ChatRequest", "ChatWorkflow", "PrepareSources"]
