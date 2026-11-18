"""One debug line per step a chat graph runs, written as the step starts.

``Chat step [graph=classic | step=retrieve | queries=2]``: the graph, the step,
and what it was given, never the user's words. With the request id the
middleware binds, the lines of one request read as its path through the graph.
"""

from __future__ import annotations

from core.utils.logging import get_logger

logger = get_logger()


def log_step(graph: str, step: str, **details: object) -> None:
    """Log that *graph* starts *step*, with *details* about what it was given."""
    logger.bind(graph=graph, step=step, **details).debug("Chat step")


__all__ = ["log_step"]
