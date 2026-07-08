"""OpenRAG configuration package.

Public API:
    load_config()  — load config (cached singleton, or fresh with overrides)
    Settings        — root Pydantic model
    get_settings()  — cached singleton accessor
"""

from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

from .root import Settings


@lru_cache
def get_settings() -> Settings:
    """Cached singleton — one Settings instance per process."""
    from .loader import load_config as _load

    return _load()


def load_config(
    config_path: str | Path | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> Settings:
    """Return the cached Pydantic Settings singleton.

    The ``config_path`` parameter is kept for backward compatibility.
    Use ``OPENRAG_CONF_DIR`` env var to override the config directory.

    The ``overrides`` parameter bypasses the cache (useful for tests).
    """
    if overrides is not None or config_path is not None:
        from .loader import load_config as _load

        return _load(conf_dir=config_path, overrides=overrides)
    return get_settings()


__all__ = ["load_config", "Settings", "get_settings"]
