"""Reranker registry."""

from openrag.core.utils.registry import Registry

from .reranker import Reranker

reranker_registry: Registry[Reranker] = Registry("reranker")
