"""Placement and per-container env for the openrag Deployment (#1037)."""

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


def _isolated_chart(tmp_path: Path) -> Path:
    """Copy only the parent-chart files needed to render openrag.yaml."""
    chart = tmp_path / "openrag-stack"
    templates = chart / "templates"
    templates.mkdir(parents=True)
    shutil.copy(CHART_DIR / "values.yaml", chart / "values.yaml")
    for name in ("_helpers.tpl", "configmap-env.yaml", "openrag.yaml"):
        shutil.copy(CHART_DIR / "templates" / name, templates / name)
    (chart / "Chart.yaml").write_text("apiVersion: v2\nname: openrag-stack\nversion: 0.0.0\n", encoding="utf-8")
    return chart


def _pod_spec(tmp_path: Path, values: dict | None = None, *args: str) -> dict:
    assert HELM is not None
    command = [HELM, "template", "test", str(_isolated_chart(tmp_path)), "--show-only", "templates/openrag.yaml"]
    if values is not None:
        values_file = tmp_path / "values.yaml"
        values_file.write_text(yaml.safe_dump(values), encoding="utf-8")
        command += ["-f", str(values_file)]
    result = subprocess.run([*command, *args], check=False, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    deployment = next(
        document for document in yaml.safe_load_all(result.stdout) if document and document.get("kind") == "Deployment"
    )
    return deployment["spec"]["template"]["spec"]


@requires_helm
def test_defaults_set_no_placement(tmp_path: Path) -> None:
    spec = _pod_spec(tmp_path)

    for key in ("nodeSelector", "tolerations", "affinity", "runtimeClassName"):
        assert key not in spec
    # Embedded Ray is the default, and pins its metrics port.
    assert [entry["name"] for entry in spec["containers"][0]["env"]] == ["RAY_METRICS_EXPORT_PORT"]
    assert spec["containers"][0]["resources"]["requests"]["nvidia.com/gpu"] == 1


@requires_helm
def test_placement_values_reach_the_pod(tmp_path: Path) -> None:
    node_selector = {"nvidia.com/gpu.present": "true"}
    tolerations = [{"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}]
    affinity = {
        "nodeAffinity": {
            "requiredDuringSchedulingIgnoredDuringExecution": {
                "nodeSelectorTerms": [
                    {"matchExpressions": [{"key": "kubernetes.io/arch", "operator": "In", "values": ["amd64"]}]}
                ]
            }
        }
    }
    spec = _pod_spec(
        tmp_path,
        {
            "openrag": {
                "nodeSelector": node_selector,
                "tolerations": tolerations,
                "affinity": affinity,
                "runtimeClassName": "nvidia",
            }
        },
    )

    assert spec["nodeSelector"] == node_selector
    assert spec["tolerations"] == tolerations
    assert spec["affinity"] == affinity
    assert spec["runtimeClassName"] == "nvidia"


@requires_helm
def test_runtime_class_sharing_drops_the_gpu_request(tmp_path: Path) -> None:
    """The documented runtime-class setup: no nvidia.com/gpu request, and the
    card exposed through NVIDIA_VISIBLE_DEVICES on the API container."""
    spec = _pod_spec(
        tmp_path,
        {
            "openrag": {
                "runtimeClassName": "nvidia",
                "resources": {"requests": {"nvidia.com/gpu": None}, "limits": {"nvidia.com/gpu": None}},
                "extraEnv": [{"name": "NVIDIA_VISIBLE_DEVICES", "value": "all"}],
            }
        },
    )
    container = spec["containers"][0]

    assert "nvidia.com/gpu" not in container["resources"]["requests"]
    assert "nvidia.com/gpu" not in container["resources"]["limits"]
    assert container["resources"]["requests"]["memory"] == "16Gi"
    assert {"name": "NVIDIA_VISIBLE_DEVICES", "value": "all"} in container["env"]


@requires_helm
def test_extra_env_follows_the_embedded_ray_metrics_port(tmp_path: Path) -> None:
    spec = _pod_spec(tmp_path, {"openrag": {"extraEnv": [{"name": "NVIDIA_VISIBLE_DEVICES", "value": "all"}]}})

    assert [entry["name"] for entry in spec["containers"][0]["env"]] == [
        "RAY_METRICS_EXPORT_PORT",
        "NVIDIA_VISIBLE_DEVICES",
    ]


@requires_helm
def test_extra_env_alone_without_embedded_ray(tmp_path: Path) -> None:
    """Pointed at an external Ray, the API sets no metrics port: extraEnv is
    then the whole env list."""
    spec = _pod_spec(
        tmp_path,
        {"openrag": {"extraEnv": [{"name": "NVIDIA_VISIBLE_DEVICES", "value": "all"}]}},
        "--set",
        "env.config.RAY_ADDRESS=ray://head:10001",
    )

    assert spec["containers"][0]["env"] == [{"name": "NVIDIA_VISIBLE_DEVICES", "value": "all"}]


@requires_helm
def test_no_env_without_embedded_ray_or_extra_env(tmp_path: Path) -> None:
    spec = _pod_spec(tmp_path, None, "--set", "env.config.RAY_ADDRESS=ray://head:10001")

    assert "env" not in spec["containers"][0]
