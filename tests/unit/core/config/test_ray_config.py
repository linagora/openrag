from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from core.config.infrastructure import RayIndexerConfig
from pydantic import ValidationError

_REPO_ROOT = Path(__file__).resolve().parents[4]


def test_ray_indexer_config_defaults() -> None:
    cfg = RayIndexerConfig()
    assert cfg.pool_size == 1
    assert cfg.max_tasks_per_worker == 10


def test_shipped_files_in_flight_default_matches_the_model() -> None:
    """The app loads conf/config.yaml and the chart sets the env, so a default only in the model never applies."""
    conf = yaml.safe_load((_REPO_ROOT / "conf/config.yaml").read_text())
    values = yaml.safe_load((_REPO_ROOT / "infra/charts/openrag-stack/values.yaml").read_text())
    assert conf["ray"]["indexer"]["max_tasks_per_worker"] == 10
    assert values["env"]["config"]["RAY_MAX_TASKS_PER_WORKER"] == "10"


@pytest.mark.parametrize("field", ["pool_size", "max_tasks_per_worker"])
def test_ray_indexer_config_rejects_sub_one(field: str) -> None:
    # The runtime max(1, ...) floor was replaced by ge=1 validation at the boundary.
    with pytest.raises(ValidationError):
        RayIndexerConfig(**{field: 0})
