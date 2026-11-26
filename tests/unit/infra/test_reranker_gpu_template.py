"""Render coverage for the NVIDIA variables the reranker template sets.

The chart sets ``NVIDIA_VISIBLE_DEVICES`` and ``NVIDIA_DRIVER_CAPABILITIES`` to
the values Infinity's image sets in its ENV, so the reranker's GPU no longer
depends on the image. It leaves a variable to a value of the user's, and the
device list to the device plugin when the reranker requests an NVIDIA resource.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
CHART_DIR = ROOT / "infra" / "charts" / "openrag-stack"
HELM = os.environ.get("HELM_BIN") or shutil.which("helm")
requires_helm = pytest.mark.skipif(HELM is None, reason="Helm is not installed")

# What michaelf34/infinity:0.0.77 sets in its image ENV.
IMAGE_ENV = [
    {"name": "NVIDIA_VISIBLE_DEVICES", "value": "all"},
    {"name": "NVIDIA_DRIVER_CAPABILITIES", "value": "compute,utility"},
]


@pytest.fixture
def chart(tmp_path: Path) -> Path:
    """Copy only the parent-chart files needed to render infinity.yaml."""
    chart = tmp_path / "openrag-stack"
    templates = chart / "templates"
    templates.mkdir(parents=True)
    shutil.copy(CHART_DIR / "values.yaml", chart / "values.yaml")
    shutil.copy(CHART_DIR / "templates" / "_helpers.tpl", templates / "_helpers.tpl")
    shutil.copy(CHART_DIR / "templates" / "infinity.yaml", templates / "infinity.yaml")
    (chart / "Chart.yaml").write_text(
        "apiVersion: v2\nname: openrag-stack\nversion: 0.0.0\n",
        encoding="utf-8",
    )
    return chart


def _pod_spec(chart: Path, *args: str) -> dict:
    assert HELM is not None
    result = subprocess.run(
        [HELM, "template", "test", str(chart), "--show-only", "templates/infinity.yaml", *args],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    deployment = next(
        document for document in yaml.safe_load_all(result.stdout) if document and document.get("kind") == "Deployment"
    )
    return deployment["spec"]["template"]["spec"]


def _env(pod: dict) -> list[dict]:
    env = pod["containers"][0].get("env", [])
    names = [entry["name"] for entry in env]
    # A name listed twice is left to how each apply merges the list.
    assert len(names) == len(set(names)), names
    return env


@requires_helm
def test_defaults_set_the_image_values_and_nothing_else(chart: Path) -> None:
    pod = _pod_spec(chart)

    assert _env(pod) == IMAGE_ENV
    # Placement is unchanged: without a runtime class the reranker runs on CPU.
    assert "runtimeClassName" not in pod
    assert "nodeSelector" not in pod
    assert "nvidia.com/gpu" not in pod["containers"][0]["resources"].get("limits", {})


@requires_helm
def test_linagora_values_share_the_gpu(chart: Path) -> None:
    pod = _pod_spec(chart, "-f", str(CHART_DIR / "values-linagora.yaml"))

    assert pod["runtimeClassName"] == "nvidia"
    assert pod["nodeSelector"] == {"gpu-role": "serving"}
    assert _env(pod) == IMAGE_ENV


@requires_helm
def test_extra_env_follows_the_chart_variables(chart: Path) -> None:
    pod = _pod_spec(
        chart,
        "--set",
        "reranker.extraEnv[0].name=INFINITY_MODEL_WARMUP",
        "--set-string",
        "reranker.extraEnv[0].value=false",
    )

    assert _env(pod) == [*IMAGE_ENV, {"name": "INFINITY_MODEL_WARMUP", "value": "false"}]


@requires_helm
@pytest.mark.parametrize("name", ["NVIDIA_VISIBLE_DEVICES", "NVIDIA_DRIVER_CAPABILITIES"])
def test_extra_env_replaces_the_chart_value(chart: Path, name: str) -> None:
    pod = _pod_spec(chart, "--set", f"reranker.extraEnv[0].name={name}", "--set-string", "reranker.extraEnv[0].value=0")

    others = [entry for entry in IMAGE_ENV if entry["name"] != name]
    assert _env(pod) == [*others, {"name": name, "value": "0"}]


@requires_helm
@pytest.mark.parametrize("name", ["NVIDIA_VISIBLE_DEVICES", "NVIDIA_DRIVER_CAPABILITIES"])
def test_env_config_replaces_the_chart_value(chart: Path, name: str) -> None:
    """env.config reaches the container through envFrom, which its env would override."""
    pod = _pod_spec(chart, "--set-string", f"env.config.{name}=0")

    assert _env(pod) == [entry for entry in IMAGE_ENV if entry["name"] != name]


@requires_helm
@pytest.mark.parametrize(
    "resource",
    [
        "requests.nvidia\\.com/gpu",
        "limits.nvidia\\.com/gpu",
        "limits.nvidia\\.com/mig-1g\\.5gb",
        "limits.nvidia\\.com/gpu\\.shared",
    ],
)
def test_requested_nvidia_resource_keeps_the_device_plugin_list(chart: Path, resource: str) -> None:
    """The container's env would replace the device the plugin assigned."""
    pod = _pod_spec(chart, "--set", f"reranker.resources.{resource}=1")

    assert _env(pod) == [{"name": "NVIDIA_DRIVER_CAPABILITIES", "value": "compute,utility"}]
    section, _, name = resource.partition(".")
    assert pod["containers"][0]["resources"][section][name.replace("\\", "")] == 1
