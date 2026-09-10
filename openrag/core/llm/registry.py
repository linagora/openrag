"""LLM registry."""

from core.utils.registry import Registry

from .llm import LLM

llm_registry: Registry[LLM] = Registry("llm")
