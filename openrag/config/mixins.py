"""ConfigMixin and environment variable helpers for Pydantic config models."""

from __future__ import annotations

import os
from typing import Any

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# ConfigMixin — backward-compatible dict-like access on Pydantic models
# ---------------------------------------------------------------------------
class ConfigMixin(BaseModel):
    """Mixin that gives Pydantic models dict-like behaviour so that existing
    code using ``config.section.get("key")``, ``config.section["key"]``,
    ``dict(config.section)``, and ``**config.section`` keeps working.
    """

    model_config = {"extra": "allow"}

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return getattr(self, key)
        except AttributeError:
            return default

    def __getitem__(self, key: str) -> Any:
        try:
            return getattr(self, key)
        except AttributeError:
            raise KeyError(key)

    def keys(self):
        return list(self.model_fields.keys())

    def values(self):
        return [getattr(self, k) for k in self.model_fields]

    def items(self):
        return [(k, getattr(self, k)) for k in self.model_fields]

    def __iter__(self):
        return iter(self.model_fields)

    def __contains__(self, key: str) -> bool:
        return key in self.model_fields


# ---------------------------------------------------------------------------
# Helpers to read env vars with optional defaults, coercing types.
# ---------------------------------------------------------------------------
def _env(var: str, default: Any = None) -> Any:
    """Read an environment variable, returning *default* if unset/empty."""
    val = os.environ.get(var)
    if val is None or val == "":
        return default
    return val


def _env_bool(var: str, default: bool = False) -> bool:
    val = os.environ.get(var)
    if val is None or val == "":
        return default
    return val.lower() in ("true", "1", "yes")


def _env_int(var: str, default: int = 0) -> int:
    val = os.environ.get(var)
    if val is None or val == "":
        return default
    return int(val)


def _env_float(var: str, default: float = 0.0) -> float:
    val = os.environ.get(var)
    if val is None or val == "":
        return default
    return float(val)
