"""LLM registry."""

from openrag.core.llm.llm import LLM
from openrag.core.utils.registry import Registry

llm_registry: Registry[LLM] = Registry("llm")
