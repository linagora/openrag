"""Chart guards for the embedded Ray (#1364).

With ``ray.enabled: false`` and no ``RAY_ADDRESS``, each openrag pod starts its
own Ray. Two settings then break indexing without any error at install time:
several replicas (each with its own task state), and a Marker pool reserving
more GPU than the pod has. The chart refuses both.
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

NO_GPU_RESOURCES = '{"requests":{"nvidia.com/gpu":null},"limits":{"nvidia.com/gpu":null}}'
SHARED_RAY = ("--set", "ray.enabled=true", "--set", "env.config.RAY_ADDRESS=ray://head:10001")


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


def _render(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    assert HELM is not None
    return subprocess.run(
        [HELM, "template", "test", str(_isolated_chart(tmp_path)), "--show-only", "templates/openrag.yaml", *args],
        check=False,
        capture_output=True,
        text=True,
    )


def test_ray_num_gpus_is_gone_from_the_chart() -> None:
    """Set in the chart for years and read nowhere in the app."""
    values = yaml.safe_load((CHART_DIR / "values.yaml").read_text(encoding="utf-8"))
    assert "RAY_NUM_GPUS" not in values["env"]["config"]


@requires_helm
def test_defaults_render(tmp_path: Path) -> None:
    result = _render(tmp_path)
    assert result.returncode == 0, result.stderr


@requires_helm
def test_several_replicas_with_the_embedded_ray_are_refused(tmp_path: Path) -> None:
    result = _render(tmp_path, "--set", "openrag.replicas=2")
    assert result.returncode != 0
    assert "openrag.replicas=2 with the embedded Ray" in result.stderr


@requires_helm
@pytest.mark.parametrize(
    "shared_ray",
    [
        SHARED_RAY,
        # An external Ray cluster without the chart's RayCluster.
        ("--set", "env.config.RAY_ADDRESS=ray://external:10001"),
    ],
    ids=["chart-ray-cluster", "external-ray"],
)
def test_several_replicas_sharing_a_ray_cluster_render(tmp_path: Path, shared_ray: tuple[str, ...]) -> None:
    result = _render(tmp_path, "--set", "openrag.replicas=2", *shared_ray)
    assert result.returncode == 0, result.stderr


@requires_helm
def test_a_marker_pool_larger_than_the_pod_gpus_is_refused(tmp_path: Path) -> None:
    # 2 x 0.6 (the chart's MARKER_NUM_GPUS) = 1.2 GPU on a 1-GPU pod.
    result = _render(tmp_path, "--set-string", "env.config.MARKER_POOL_SIZE=2")
    assert result.returncode != 0
    assert "MARKER_POOL_SIZE (2) x MARKER_NUM_GPUS (0.6) reserves 1.2 GPU, but the openrag pod has 1" in result.stderr


@requires_helm
@pytest.mark.parametrize(
    "args",
    [
        # 2 x 0.5 fits a 1-GPU pod exactly.
        ("--set-string", "env.config.MARKER_POOL_SIZE=2", "--set-string", "env.config.MARKER_NUM_GPUS=0.5"),
        # Without a GPU in the pod, Marker reserves none.
        ("--set-string", "env.config.MARKER_POOL_SIZE=3", "--set-json", f"openrag.resources={NO_GPU_RESOURCES}"),
        # On a shared Ray cluster, Marker runs on the Ray worker pods.
        ("--set-string", "env.config.MARKER_POOL_SIZE=3", *SHARED_RAY),
    ],
    ids=["fits-exactly", "no-gpu-in-pod", "shared-ray"],
)
def test_marker_pools_that_fit_render(tmp_path: Path, args: tuple[str, ...]) -> None:
    result = _render(tmp_path, *args)
    assert result.returncode == 0, result.stderr
