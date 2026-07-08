"""Base config mixin — frozen Pydantic models with dict-like backward compatibility."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class ConfigMixin(BaseModel):
    """Frozen Pydantic model with dict-like access for backward compatibility.

    Existing code using ``config.section.get("key")``, ``config.section["key"]``,
    ``dict(config.section)``, and ``**config.section`` keeps working.
    """

    model_config = {"frozen": True}

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
        return list(type(self).model_fields.keys())

    def values(self):
        return [getattr(self, k) for k in type(self).model_fields]

    def items(self):
        return [(k, getattr(self, k)) for k in type(self).model_fields]

    def __iter__(self):
        return iter(type(self).model_fields)

    def __contains__(self, key: str) -> bool:
        return key in type(self).model_fields
