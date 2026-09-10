"""Compressor ABC + registry."""

from core.models.compression import CompressionOptions, CompressionResult

from .compressor import Compressor
from .noop import NoopCompressor
from .registry import compressor_registry

__all__ = [
    "CompressionOptions",
    "CompressionResult",
    "Compressor",
    "NoopCompressor",
    "compressor_registry",
]
