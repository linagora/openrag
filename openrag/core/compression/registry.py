"""Compressor registry."""

from core.utils.registry import Registry

from .compressor import Compressor

compressor_registry: Registry[Compressor] = Registry("compressor")
