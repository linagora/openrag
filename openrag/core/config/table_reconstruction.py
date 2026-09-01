"""Configuration for optional cross-page PDF table reconstruction."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

TABLE_RECONSTRUCTION_MODES: tuple[str, ...] = ("disabled", "automatic")
TABLE_RECONSTRUCTION_ALGORITHM_VERSION = "adjacent-layout-v1"


class TableReconstructionConfig(BaseModel):
    """Conservative policy for reconstructing logical rows across PDF pages."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["disabled", "automatic"] = "disabled"
    same_table_min_confidence: float = Field(default=0.90, ge=0.80, le=1.00)
    row_continuation_min_confidence: float = Field(default=0.90, ge=0.80, le=1.00)
    cell_assignment_min_confidence: float = Field(default=0.90, ge=0.80, le=1.00)
    algorithm_version: Literal["adjacent-layout-v1"] = TABLE_RECONSTRUCTION_ALGORITHM_VERSION


__all__ = [
    "TABLE_RECONSTRUCTION_ALGORITHM_VERSION",
    "TABLE_RECONSTRUCTION_MODES",
    "TableReconstructionConfig",
]
