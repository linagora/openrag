"""One debug line per step a chat graph runs, written as the step starts.

``Chat step [graph=agentic | step=tools | round=1 | calls=1 | tools=search_knowledge {"queries": ["notice period"]}]``:
the graph, the step, and what it was given; the agent's tool calls with
their arguments, which hold the queries the model wrote. With the request id
the middleware binds, the lines of one request read as its path through the
graph.
"""

from __future__ import annotations

from core.utils.logging import get_logger

logger = get_logger()


def log_step(graph: str, step: str, **details: object) -> None:
    """Log that *graph* starts *step*, with *details* about what it was given."""
    logger.bind(graph=graph, step=step, **details).debug("Chat step")


__all__ = ["log_step"]
