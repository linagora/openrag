"""Retriever strategy registry."""

from __future__ import annotations

from typing import TYPE_CHECKING

from openrag.core.utils.registry import Registry

if TYPE_CHECKING:
    from .retriever import Retriever

retriever_registry: Registry[Retriever] = Registry("retriever")
