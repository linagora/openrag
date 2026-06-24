from __future__ import annotations

import pytest
from core.config.infrastructure import RayIndexerConfig
from pydantic import ValidationError


def test_ray_indexer_config_defaults() -> None:
    cfg = RayIndexerConfig()
    assert cfg.pool_size == 1
    assert cfg.max_tasks_per_worker == 50


@pytest.mark.parametrize("field", ["pool_size", "max_tasks_per_worker"])
def test_ray_indexer_config_rejects_sub_one(field: str) -> None:
    # The runtime max(1, ...) floor was replaced by ge=1 validation at the boundary.
    with pytest.raises(ValidationError):
        RayIndexerConfig(**{field: 0})
