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
    shutil.copy(CHART_DIR / "templates" / "_helpers.tpl", templates / "_helpers.tpl")
    shutil.copy(
        CHART_DIR / "templates" / "configmap-env.yaml",
        templates / "configmap-env.yaml",
    )
    shutil.copy(CHART_DIR / "templates" / "openrag.yaml", templates / "openrag.yaml")
    (chart / "Chart.yaml").write_text(
        "apiVersion: v2\nname: openrag-stack\nversion: 0.0.0\n",
        encoding="utf-8",
    )
    return chart


def _render_openrag(chart: Path, *args: str) -> subprocess.CompletedProcess[str]:
    assert HELM is not None
    return subprocess.run(
        [
            HELM,
            "template",
            "test",
            str(chart),
            "--show-only",
            "templates/openrag.yaml",
            *args,
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def _openrag_container(rendered: str) -> dict:
    deployment = next(
        document for document in yaml.safe_load_all(rendered) if document and document.get("kind") == "Deployment"
    )
    return deployment["spec"]["template"]["spec"]["containers"][0]


@requires_helm
def test_direct_api_uses_http_probes(tmp_path: Path) -> None:
    result = _render_openrag(_isolated_chart(tmp_path))

    assert result.returncode == 0, result.stderr
    container = _openrag_container(result.stdout)
    assert container["startupProbe"]["httpGet"]["path"] == "/health_check"
    assert container["readinessProbe"]["httpGet"]["path"] == "/ready"
    assert container["livenessProbe"]["httpGet"]["path"] == "/health_check"


@requires_helm
def test_ray_serve_uses_exec_probes_against_ray_head(tmp_path: Path) -> None:
    result = _render_openrag(
        _isolated_chart(tmp_path),
        "--set",
        "ray.enabled=true",
        "--set-string",
        "env.config.ENABLE_RAY_SERVE=true",
    )

    assert result.returncode == 0, result.stderr
    container = _openrag_container(result.stdout)
    expected_base = "http://openrag-raycluster-head-svc:80"
    assert container["startupProbe"]["exec"]["command"][-1] == f"{expected_base}/ready"
    assert container["readinessProbe"]["exec"]["command"][-1] == f"{expected_base}/ready"
    assert container["livenessProbe"]["exec"]["command"][-1] == f"{expected_base}/health_check"


@requires_helm
def test_ray_serve_requires_the_ray_cluster(tmp_path: Path) -> None:
    result = _render_openrag(
        _isolated_chart(tmp_path),
        "--set-string",
        "env.config.ENABLE_RAY_SERVE=true",
    )

    assert result.returncode != 0
    assert "ENABLE_RAY_SERVE=true requires ray.enabled=true" in result.stderr
