"""Chart guards for the embedded Ray (#1364).

With ``ray.enabled: false`` and no ``RAY_ADDRESS``, each openrag pod starts its
own Ray. Two settings then break indexing without any error at install time:
several replicas (each with its own task state), and parser workers (Marker,
Docling, Whisper) reserving more GPU than the pod's cards can place. The chart
refuses both, and replaces the pod instead of rolling it, which would briefly
run two.
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
    # The remedy names everything a shared Ray needs: ray.enabled alone fails to render.
    assert "ray.enabled=true together with env.config.RAY_ADDRESS or env.config.ENABLE_RAY_SERVE=true" in result.stderr


@requires_helm
@pytest.mark.parametrize(
    "shared_ray",
    [
        SHARED_RAY,
        # An external Ray cluster without the chart's RayCluster.
        ("--set", "env.config.RAY_ADDRESS=ray://external:10001"),
        # RAY_ADDRESS from a Secret the chart cannot read, declared by ray.externalCluster.
        ("--set", "ray.externalCluster=true"),
    ],
    ids=["chart-ray-cluster", "external-ray", "external-ray-from-secret"],
)
def test_several_replicas_sharing_a_ray_cluster_render(tmp_path: Path, shared_ray: tuple[str, ...]) -> None:
    result = _render(tmp_path, "--set", "openrag.replicas=2", *shared_ray)
    assert result.returncode == 0, result.stderr


def _gpus(count: int) -> tuple[str, ...]:
    return ("--set-json", f'openrag.resources={{"limits":{{"nvidia.com/gpu":{count}}}}}')


@requires_helm
@pytest.mark.parametrize(
    ("args", "worker"),
    [
        # 2 x 0.6 (the chart's MARKER_NUM_GPUS) on a 1-GPU pod.
        (("--set-string", "env.config.MARKER_POOL_SIZE=2"), "Marker"),
        # 1.8 GPU in total, but only one 0.6 fits on each card: the third never starts.
        (("--set-string", "env.config.MARKER_POOL_SIZE=3", *_gpus(2)), "Marker"),
        # Marker takes the whole card, and Docling and Whisper reserve 0.01 each.
        (("--set-string", "env.config.MARKER_NUM_GPUS=1"), "Docling"),
        (("--set-string", "env.config.MARKER_NUM_GPUS=1", "--set-string", "env.config.DOCLING_NUM_GPUS=0"), "Whisper"),
        # env.secrets wins over env.config at runtime.
        (("--set-string", "env.secrets.MARKER_POOL_SIZE=2"), "Marker"),
        # openrag.extraEnv lands in the container env, which wins over envFrom.
        (("--set-json", 'openrag.extraEnv=[{"name":"MARKER_NUM_GPUS","value":"2"}]'), "Marker"),
        # Fits if Docling starts first (0.6 + 0.4 on each card), but not if Marker
        # does: both 0.4 land on the first card and the second 0.6 has no room.
        (
            (
                *("--set-string", "env.config.MARKER_POOL_SIZE=2", "--set-string", "env.config.MARKER_NUM_GPUS=0.4"),
                *("--set-string", "env.config.DOCLING_POOL_SIZE=2", "--set-string", "env.config.DOCLING_NUM_GPUS=0.6"),
                *("--set-string", "env.config.WHISPER_NUM_GPUS=0", *_gpus(2)),
            ),
            "Docling",
        ),
    ],
    ids=[
        "pool-over-one-gpu",
        "fraction-per-card",
        "docling-counts",
        "whisper-counts",
        "pool-from-secrets",
        "gpus-from-extra-env",
        "start-order",
    ],
)
def test_gpu_workers_that_do_not_fit_the_pod_are_refused(tmp_path: Path, args: tuple[str, ...], worker: str) -> None:
    result = _render(tmp_path, *args)
    assert result.returncode != 0
    assert "The GPU workers do not fit the openrag pod's" in result.stderr
    assert f"a {worker} worker never starts" in result.stderr


@requires_helm
def test_a_fractional_reservation_above_one_gpu_is_refused(tmp_path: Path) -> None:
    result = _render(tmp_path, "--set-string", "env.config.MARKER_NUM_GPUS=1.5", *_gpus(2))
    assert result.returncode != 0
    assert "MARKER_NUM_GPUS=1.5: Ray accepts a GPU reservation above 1 only as a whole number" in result.stderr


@requires_helm
@pytest.mark.parametrize(
    "args",
    [
        # 0.9 for Marker and 0.03 for Docling and Whisper on one card.
        ("--set-string", "env.config.MARKER_POOL_SIZE=2", "--set-string", "env.config.MARKER_NUM_GPUS=0.45"),
        # One 0.6 on each card.
        ("--set-string", "env.config.MARKER_POOL_SIZE=2", *_gpus(2)),
        # A whole card for Marker, the other for Docling and Whisper.
        ("--set-string", "env.config.MARKER_NUM_GPUS=1", *_gpus(2)),
        # Without a GPU in the pod, the workers reserve none.
        ("--set-string", "env.config.MARKER_POOL_SIZE=3", "--set-json", f"openrag.resources={NO_GPU_RESOURCES}"),
        # On a shared Ray cluster, the workers run on the Ray worker pods.
        ("--set-string", "env.config.MARKER_POOL_SIZE=3", *SHARED_RAY),
        ("--set-string", "env.config.MARKER_POOL_SIZE=3", "--set", "ray.externalCluster=true"),
        # env.secrets reaches the pod only through the Secret the chart renders:
        # beside another Secret it is a leftover the workers never read.
        ("--set-string", "env.secrets.MARKER_POOL_SIZE=2", "--set", "env.existingSecret=openrag-env"),
        ("--set-string", "env.secrets.MARKER_POOL_SIZE=2", "--set", "env.secretsProvider.type=externalSecret"),
    ],
    ids=[
        "fits-one-card",
        "one-per-card",
        "whole-card-each",
        "no-gpu-in-pod",
        "shared-ray",
        "external-ray-from-secret",
        "secrets-beside-an-existing-secret",
        "secrets-beside-an-operator-secret",
    ],
)
def test_gpu_workers_that_fit_render(tmp_path: Path, args: tuple[str, ...]) -> None:
    result = _render(tmp_path, *args)
    assert result.returncode == 0, result.stderr


@requires_helm
@pytest.mark.parametrize(
    ("args", "recreate"),
    [((), True), (SHARED_RAY, False)],
    ids=["embedded-ray", "shared-ray"],
)
def test_the_embedded_ray_never_runs_two_pods_during_a_rollout(
    tmp_path: Path, args: tuple[str, ...], recreate: bool
) -> None:
    """A rolling update would run two pods, each with its own Ray and job reconciler."""
    result = _render(tmp_path, *args)
    assert result.returncode == 0, result.stderr
    (deployment,) = [doc for doc in yaml.safe_load_all(result.stdout) if doc and doc["kind"] == "Deployment"]
    assert (deployment["spec"].get("strategy") == {"type": "Recreate"}) is recreate
