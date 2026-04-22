"""Reranker registry."""

from openrag.core.rerankers.reranker import Reranker
from openrag.core.utils.registry import Registry

reranker_registry: Registry[Reranker] = Registry("reranker")
