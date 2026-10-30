"""Every deployment that switches to LOG_FORMAT=json also sets what keeps the
stream JSON: Ray's relay settings and no tqdm progress bars (#1297)."""

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
JSON_LOGS_ENV = {"LOG_FORMAT": "json", "RAY_DEDUP_LOGS": "0", "RAY_COLOR_PREFIX": "0", "TQDM_DISABLE": "1"}


def _overlay_env(service: str) -> dict:
    compose = yaml.safe_load((REPO_ROOT / "infra/compose/logging.docker-compose.yaml").read_text())
    return compose["services"][service]["environment"]


def _chart_env() -> dict:
    return yaml.safe_load((REPO_ROOT / "infra/charts/openrag-stack/values.yaml").read_text())["env"]["config"]


@pytest.mark.parametrize(
    "env",
    [_overlay_env("openrag"), _overlay_env("openrag-cpu"), _chart_env()],
    ids=["compose-openrag", "compose-openrag-cpu", "chart"],
)
def test_json_logs_come_with_their_companion_env(env):
    assert {key: env.get(key) for key in JSON_LOGS_ENV} == JSON_LOGS_ENV
