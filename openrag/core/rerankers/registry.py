"""Reranker registry."""

from core.utils.registry import Registry

from .reranker import Reranker

reranker_registry: Registry[Reranker] = Registry("reranker")
