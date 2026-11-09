"""Every deployment that switches to LOG_FORMAT=json also sets what keeps the
stream JSON: Ray's relay settings and no tqdm progress bars (#1297)."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
CHART_DIR = REPO_ROOT / "infra/charts/openrag-stack"
HELM = os.environ.get("HELM_BIN") or shutil.which("helm")
JSON_LOGS_ENV = {"LOG_FORMAT": "json", "RAY_DEDUP_LOGS": "0", "RAY_COLOR_PREFIX": "0", "TQDM_DISABLE": "1"}


@pytest.mark.parametrize("service", ["openrag", "openrag-cpu"])
def test_compose_overlay_sets_the_companion_env(service):
    compose = yaml.safe_load((REPO_ROOT / "infra/compose/logging.docker-compose.yaml").read_text())
    env = compose["services"][service]["environment"]

    assert {key: env.get(key) for key in JSON_LOGS_ENV} == JSON_LOGS_ENV


def _chart_env(tmp_path: Path, *args: str) -> dict:
    chart = tmp_path / "openrag-stack"
    (chart / "templates").mkdir(parents=True)
    shutil.copy(CHART_DIR / "values.yaml", chart / "values.yaml")
    for template in ("_helpers.tpl", "configmap-env.yaml"):
        shutil.copy(CHART_DIR / "templates" / template, chart / "templates" / template)
    (chart / "Chart.yaml").write_text("apiVersion: v2\nname: openrag-stack\nversion: 0.0.0\n")
    result = subprocess.run(
        [HELM, "template", "test", str(chart), "--show-only", "templates/configmap-env.yaml", *args],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return yaml.safe_load(result.stdout)["data"]


@pytest.mark.skipif(HELM is None, reason="Helm is not installed")
@pytest.mark.parametrize(
    ("args", "tqdm_disable"),
    [
        ((), "1"),
        (("--set-string", "env.config.LOG_FORMAT=JSON"), "1"),
        (("--set-string", "env.config.LOG_FORMAT=text"), None),
        (("--set-string", "env.config.TQDM_DISABLE="), ""),
    ],
    ids=["default-json", "uppercase-json", "text", "explicit-override"],
)
def test_chart_disables_tqdm_with_json_logs(tmp_path, args, tqdm_disable):
    env = _chart_env(tmp_path, *args)

    assert {key: env.get(key) for key in ("RAY_DEDUP_LOGS", "RAY_COLOR_PREFIX")} == {
        "RAY_DEDUP_LOGS": "0",
        "RAY_COLOR_PREFIX": "0",
    }
    assert env.get("TQDM_DISABLE") == tqdm_disable
