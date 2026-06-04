"""In-memory preset cache — populated from DB by PresetService.load_all()."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from .base import ConfigMixin


class PresetsConfig(ConfigMixin):
    """Named preset dicts — one dict per preset type.

    Like ModelsConfig, the fields are frozen but the dicts they hold
    are mutable. PresetService.load_all() performs clear()+update()
    for atomic in-place swaps.
    """

    indexation: dict[str, dict[str, Any]] = Field(default_factory=dict)
    retrieval: dict[str, dict[str, Any]] = Field(default_factory=dict)


__all__ = ["PresetsConfig"]
