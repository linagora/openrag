"""``openrag.seedUsers`` reaches the API as ``SEED_USERS`` in the env ConfigMap (#1153).

``conf/config.yaml`` is baked into the image, so the chart cannot hand the
accounts over through it. The env ConfigMap is read by the API Deployment and
the Ray head alike, and its checksum already rolls the Deployment on change.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from core.config.auth import AuthConfig

ROOT = Path(__file__).resolve().parents[3]
CHART_DIR = ROOT / "infra" / "charts" / "openrag-stack"
HELM = os.environ.get("HELM_BIN") or shutil.which("helm")
pytestmark = pytest.mark.skipif(HELM is None, reason="Helm is not installed")

SEED = {
    "external_user_id": "svc-cozy-stack",
    "display_name": "cozy-stack",
    "token_env": "COZY_STACK_TOKEN",
    "partitions": [{"name": "twake", "role": "editor"}],
}


def _chart(tmp_path: Path) -> Path:
    chart = tmp_path / "openrag-stack"
    templates = chart / "templates"
    templates.mkdir(parents=True)
    shutil.copy(CHART_DIR / "values.yaml", chart / "values.yaml")
    for name in ("_helpers.tpl", "configmap-env.yaml"):
        shutil.copy(CHART_DIR / "templates" / name, templates / name)
    (chart / "Chart.yaml").write_text("apiVersion: v2\nname: openrag-stack\nversion: 0.0.0\n", encoding="utf-8")
    return chart


def _render(tmp_path: Path, values: dict | None = None) -> subprocess.CompletedProcess[str]:
    args = [HELM, "template", "test", str(_chart(tmp_path)), "--show-only", "templates/configmap-env.yaml"]
    if values is not None:
        values_file = tmp_path / "override.yaml"
        values_file.write_text(yaml.safe_dump(values), encoding="utf-8")
        args += ["-f", str(values_file)]
    return subprocess.run(args, check=False, capture_output=True, text=True)


def _data(result: subprocess.CompletedProcess[str]) -> dict:
    assert result.returncode == 0, result.stderr
    return yaml.safe_load(result.stdout)["data"]


def test_not_rendered_by_default(tmp_path):
    assert "SEED_USERS" not in _data(_render(tmp_path))


def test_rendered_as_json_the_app_validates(tmp_path):
    data = _data(_render(tmp_path, {"openrag": {"seedUsers": [SEED]}}))

    parsed = json.loads(data["SEED_USERS"])
    assert parsed == [SEED]
    config = AuthConfig(seed_users=parsed)
    assert config.seed_users[0].partitions[0].role.value == "editor"


def test_env_config_cannot_also_set_it(tmp_path):
    result = _render(tmp_path, {"openrag": {"seedUsers": [SEED]}, "env": {"config": {"SEED_USERS": "[]"}}})

    assert result.returncode != 0
    assert "SEED_USERS" in result.stderr


SECRET = "or-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"


@pytest.mark.parametrize(
    ("entry", "key"),
    [
        ({**SEED, "token": SECRET}, "token"),
        ({**SEED, "api_key": SECRET}, "api_key"),
        ({**SEED, "partitions": [{"name": "twake", "role": "editor", "token": SECRET}]}, "token"),
    ],
    ids=["token", "any-other-key", "inside-partitions"],
)
def test_an_unknown_key_fails_the_render(tmp_path, entry, key):
    """Only the documented keys may reach the ConfigMap: anything else could be a secret."""
    result = _render(tmp_path, {"openrag": {"seedUsers": [SEED, entry]}})

    assert result.returncode != 0
    assert key in result.stderr
    assert "token_env" in result.stderr
    assert SECRET not in result.stderr


@pytest.mark.parametrize("entry", [SECRET, {**SEED, "partitions": [SECRET]}], ids=["entry", "partition"])
def test_a_non_map_item_fails_the_render(tmp_path, entry):
    result = _render(tmp_path, {"openrag": {"seedUsers": [SEED, entry]}})

    assert result.returncode != 0
    assert "openrag.seedUsers" in result.stderr
    assert SECRET not in result.stderr


def test_every_documented_key_renders(tmp_path):
    full = {**SEED, "is_admin": True}
    data = _data(_render(tmp_path, {"openrag": {"seedUsers": [full]}}))

    assert json.loads(data["SEED_USERS"]) == [full]
