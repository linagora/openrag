"""LLM ABC + registry."""

from openrag.core.llm.llm import LLM
from openrag.core.llm.registry import llm_registry

__all__ = ["LLM", "llm_registry"]
