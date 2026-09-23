"""Render coverage for ``reranker.gpu.mode`` in the Infinity reranker template.

Each mode must leave the rendered manifest saying how the reranker reaches a
GPU, whatever the image's ENV or a leftover ``nvidia.com/gpu`` entry in
``reranker.resources`` would otherwise do.
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

GPU = "nvidia.com/gpu"
# A custom resources block that still carries a GPU request, as an override
# written before gpu.mode existed would.
LEFTOVER_GPU = (
    "--set",
    r"reranker.resources.requests.nvidia\.com/gpu=1",
    "--set",
    r"reranker.resources.limits.nvidia\.com/gpu=1",
)


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


def _render(chart: Path, *args: str) -> subprocess.CompletedProcess[str]:
    assert HELM is not None
    return subprocess.run(
        [HELM, "template", "test", str(chart), "--show-only", "templates/infinity.yaml", *args],
        check=False,
        capture_output=True,
        text=True,
    )


def _pod_spec(chart: Path, *args: str) -> dict:
    result = _render(chart, *args)
    assert result.returncode == 0, result.stderr
    deployment = next(
        document for document in yaml.safe_load_all(result.stdout) if document and document.get("kind") == "Deployment"
    )
    return deployment["spec"]["template"]["spec"]


def _env(pod: dict) -> dict[str, str]:
    return {entry["name"]: entry["value"] for entry in pod["containers"][0].get("env", [])}


def _set_capabilities(value: str) -> tuple[str, str]:
    # --set splits on unescaped commas, and a capability list is comma-separated.
    escaped = value.replace(",", "\\,")
    return ("--set-string", f"reranker.gpu.driverCapabilities={escaped}")


def _gpu_quantities(pod: dict) -> dict[str, object]:
    resources = pod["containers"][0]["resources"]
    return {section: resources.get(section, {}).get(GPU) for section in ("requests", "limits")}


@requires_helm
def test_runtime_mode_shares_the_card_through_the_runtime(chart: Path) -> None:
    pod = _pod_spec(chart, *LEFTOVER_GPU)

    assert pod["runtimeClassName"] == "nvidia"
    assert pod["nodeSelector"] == {"nvidia.com/gpu.present": "true"}
    assert _env(pod) == {
        "NVIDIA_VISIBLE_DEVICES": "all",
        "NVIDIA_DRIVER_CAPABILITIES": "compute,utility",
    }
    assert _gpu_quantities(pod) == {"requests": None, "limits": None}


@requires_helm
def test_request_mode_takes_gpu_count_through_the_scheduler(chart: Path) -> None:
    pod = _pod_spec(chart, *LEFTOVER_GPU, "--set", "reranker.gpu.mode=request", "--set", "reranker.gpu.count=2")

    assert pod["runtimeClassName"] == "nvidia"
    assert pod["nodeSelector"] == {"nvidia.com/gpu.present": "true"}
    # The device plugin sets the device list; an explicit one would override it.
    assert _env(pod) == {}
    assert _gpu_quantities(pod) == {"requests": 2, "limits": 2}


@requires_helm
def test_request_mode_renders_without_a_runtime_class(chart: Path) -> None:
    """For nodes whose default runtime is NVIDIA and that have no nvidia RuntimeClass."""
    pod = _pod_spec(chart, "--set", "reranker.gpu.mode=request", "--set", "reranker.runtimeClassName=")

    assert "runtimeClassName" not in pod
    assert _gpu_quantities(pod) == {"requests": 1, "limits": 1}


@requires_helm
def test_none_mode_runs_on_cpu_without_a_device(chart: Path) -> None:
    pod = _pod_spec(chart, *LEFTOVER_GPU, "--set", "reranker.gpu.mode=none")

    assert "runtimeClassName" not in pod
    assert "nodeSelector" not in pod
    assert _env(pod) == {"NVIDIA_VISIBLE_DEVICES": "void"}
    assert _gpu_quantities(pod) == {"requests": None, "limits": None}


@requires_helm
def test_custom_node_selector_replaces_the_gpu_default(chart: Path) -> None:
    pod = _pod_spec(chart, "--set", "reranker.nodeSelector.gpu-role=serving")

    assert pod["nodeSelector"] == {"gpu-role": "serving"}


@requires_helm
def test_runtime_mode_refuses_to_render_without_a_runtime_class(chart: Path) -> None:
    result = _render(chart, "--set", "reranker.runtimeClassName=")

    assert result.returncode != 0
    assert "reranker.gpu.mode=runtime needs reranker.runtimeClassName" in result.stderr


@requires_helm
def test_unknown_gpu_mode_is_refused(chart: Path) -> None:
    result = _render(chart, "--set", "reranker.gpu.mode=cuda")

    assert result.returncode != 0
    assert 'invalid reranker.gpu.mode: "cuda"' in result.stderr


@requires_helm
@pytest.mark.parametrize("devices", ["none", "void", " VOID "])
def test_runtime_mode_refuses_a_device_list_that_exposes_no_gpu(chart: Path, devices: str) -> None:
    result = _render(chart, "--set-string", f"reranker.gpu.visibleDevices={devices}")

    assert result.returncode != 0
    assert "exposes no GPU" in result.stderr


@requires_helm
def test_runtime_mode_keeps_gpu_index_zero(chart: Path) -> None:
    """A numeric 0 is GPU index 0, not an unset value to replace with "all"."""
    pod = _pod_spec(chart, "--set", "reranker.gpu.visibleDevices=0")

    assert _env(pod)["NVIDIA_VISIBLE_DEVICES"] == "0"


@requires_helm
@pytest.mark.parametrize("count", ["0", "-1", "1.5", "abc"])
def test_request_mode_refuses_a_count_that_is_not_a_positive_integer(chart: Path, count: str) -> None:
    result = _render(chart, "--set", "reranker.gpu.mode=request", "--set", f"reranker.gpu.count={count}")

    assert result.returncode != 0
    assert "reranker.gpu.count must be a positive integer" in result.stderr


@requires_helm
@pytest.mark.parametrize("capabilities", ["utility", "graphics,video", " Utility "])
def test_runtime_mode_refuses_driver_capabilities_without_compute(chart: Path, capabilities: str) -> None:
    result = _render(chart, *_set_capabilities(capabilities))

    assert result.returncode != 0
    assert 'lacks "compute"' in result.stderr


@requires_helm
@pytest.mark.parametrize("capabilities", ["all", "utility, Compute"])
def test_runtime_mode_accepts_driver_capabilities_with_compute(chart: Path, capabilities: str) -> None:
    pod = _pod_spec(chart, *_set_capabilities(capabilities))

    assert _env(pod)["NVIDIA_DRIVER_CAPABILITIES"] == capabilities.strip()


@requires_helm
def test_gpu_node_selector_is_cleared_by_null_not_by_an_empty_map(chart: Path, tmp_path: Path) -> None:
    """Helm merges {} into the default map, so only null drops the GPU-node label."""
    empty = tmp_path / "empty.yaml"
    empty.write_text("reranker:\n  gpu:\n    nodeSelector: {}\n", encoding="utf-8")
    null = tmp_path / "null.yaml"
    null.write_text("reranker:\n  gpu:\n    nodeSelector: null\n", encoding="utf-8")

    assert _pod_spec(chart, "-f", str(empty))["nodeSelector"] == {"nvidia.com/gpu.present": "true"}
    assert "nodeSelector" not in _pod_spec(chart, "-f", str(null))
