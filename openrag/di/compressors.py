"""Registration and construction of the context compressor."""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.compression import Compressor, compressor_registry
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.config.root import Settings

logger = get_logger()


def register_compressors() -> None:
    import core.compression.noop  # noqa: F401
    import services.compression.headroom_compressor  # noqa: F401


def create_compressor(settings: Settings) -> Compressor:
    """Build the configured compressor, falling back to noop on any failure.

    A misconfigured or uninstalled backend must not stop the app from booting,
    so this degrades to passthrough and logs rather than raising.
    """
    from core.compression.noop import NoopCompressor

    cfg = settings.compression
    if not cfg.enabled or cfg.backend == "noop":
        return NoopCompressor()

    try:
        return compressor_registry.create(cfg.backend, **cfg.extra)
    except Exception as exc:
        logger.error(f"Compressor backend '{cfg.backend}' unavailable, falling back to noop: {exc}")
        return NoopCompressor()


__all__ = ["create_compressor", "register_compressors"]
