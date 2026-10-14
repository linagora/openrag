"""The bundled Infinity reranker runs in float32 with TF32 off, in Compose and the chart.

In bfloat16, or in float32 with TF32 matmuls, a document's score depends on
the other pairs in its batch, so near-tied chunks swap places between
identical requests.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
CHART_DIR = ROOT / "infra" / "charts" / "openrag-stack"
COMPOSE_INFINITY = ROOT / "extern" / "reranker" / "infinity.yaml"
HELM = os.environ.get("HELM_BIN") or shutil.which("helm")
requires_helm = pytest.mark.skipif(HELM is None, reason="Helm is not installed")


def _compose_reranker() -> dict:
    return yaml.safe_load(COMPOSE_INFINITY.read_text(encoding="utf-8"))["services"]["reranker"]


def test_compose_gpu_reranker_runs_float32_without_tf32():
    service = _compose_reranker()

    assert "--dtype ${RERANKER_DTYPE:-float32}" in service["command"]
    assert service["environment"]["NVIDIA_TF32_OVERRIDE"] == "0"


def test_compose_and_chart_default_to_the_same_dtype():
    compose_default = re.search(r"--dtype \$\{RERANKER_DTYPE:-([^}]+)\}", _compose_reranker()["command"])
    values = yaml.safe_load((CHART_DIR / "values.yaml").read_text(encoding="utf-8"))

    assert compose_default is not None
    assert values["reranker"]["dtype"] == compose_default.group(1)


def _render_reranker(tmp_path: Path, *args: str) -> dict:
    """The reranker container, from a copy of the chart without its sub-charts."""
    chart = tmp_path / "openrag-stack"
    templates = chart / "templates"
    templates.mkdir(parents=True)
    shutil.copy(CHART_DIR / "values.yaml", chart / "values.yaml")
    shutil.copy(CHART_DIR / "templates" / "_helpers.tpl", templates / "_helpers.tpl")
    shutil.copy(CHART_DIR / "templates" / "infinity.yaml", templates / "infinity.yaml")
    (chart / "Chart.yaml").write_text("apiVersion: v2\nname: openrag-stack\nversion: 0.0.0\n", encoding="utf-8")

    assert HELM is not None
    result = subprocess.run(
        [HELM, "template", "test", str(chart), "--show-only", "templates/infinity.yaml", *args],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    deployment = next(doc for doc in yaml.safe_load_all(result.stdout) if doc and doc["kind"] == "Deployment")
    return deployment["spec"]["template"]["spec"]["containers"][0]


def _dtype_arg(container: dict) -> str | None:
    args = container["args"]
    return args[args.index("--dtype") + 1] if "--dtype" in args else None


@requires_helm
def test_chart_reranker_runs_float32_without_tf32(tmp_path: Path):
    container = _render_reranker(tmp_path)

    assert _dtype_arg(container) == "float32"
    assert {"name": "NVIDIA_TF32_OVERRIDE", "value": "0"} in container["env"]


@requires_helm
def test_chart_reranker_dtype_can_be_changed(tmp_path: Path):
    assert _dtype_arg(_render_reranker(tmp_path / "bf16", "--set", "reranker.dtype=bfloat16")) == "bfloat16"


@requires_helm
def test_chart_reranker_empty_dtype_keeps_infinity_default(tmp_path: Path):
    assert _dtype_arg(_render_reranker(tmp_path / "auto", "--set-string", "reranker.dtype=")) is None
