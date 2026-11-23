"""The API pods roll when a helm upgrade changes the env Secret the chart renders (#985)."""

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
pytestmark = pytest.mark.skipif(HELM is None, reason="Helm is not installed")


@pytest.fixture
def chart(tmp_path: Path) -> Path:
    """The parent-chart files openrag.yaml needs, without the subcharts."""
    chart = tmp_path / "openrag-stack"
    templates = chart / "templates"
    templates.mkdir(parents=True)
    shutil.copy(CHART_DIR / "values.yaml", chart / "values.yaml")
    for name in ("_helpers.tpl", "configmap-env.yaml", "openrag.yaml"):
        shutil.copy(CHART_DIR / "templates" / name, templates / name)
    (chart / "Chart.yaml").write_text("apiVersion: v2\nname: openrag-stack\nversion: 0.0.0\n", encoding="utf-8")
    return chart


def _pod_annotations(chart: Path, *args: str) -> dict:
    assert HELM is not None
    rendered = subprocess.run(
        [HELM, "template", "test", str(chart), "--show-only", "templates/openrag.yaml", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    deployment = next(doc for doc in yaml.safe_load_all(rendered) if doc and doc.get("kind") == "Deployment")
    return deployment["spec"]["template"]["metadata"]["annotations"]


def test_the_secret_checksum_is_stable_and_follows_each_secret(chart: Path) -> None:
    base = ("--set", "env.secrets.AUTH_TOKEN=first-token-abcdefgh")

    first = _pod_annotations(chart, *base)["checksum/secret"]

    assert _pod_annotations(chart, *base)["checksum/secret"] == first
    assert _pod_annotations(chart, "--set", "env.secrets.AUTH_TOKEN=second-token-abcdefgh")["checksum/secret"] != first
    assert _pod_annotations(chart, *base, "--set", "env.secrets.HF_TOKEN=hf-token-abcdefgh")["checksum/secret"] != first


def test_the_secret_checksum_follows_the_bundled_postgres_password(chart: Path) -> None:
    """The Secret's POSTGRES_PASSWORD is read from postgresql.auth.password when the chart runs Postgres."""
    first = _pod_annotations(chart, "--set", "postgresql.auth.password=first-pg-password")["checksum/secret"]
    second = _pod_annotations(chart, "--set", "postgresql.auth.password=second-pg-password")["checksum/secret"]

    assert first != second


@pytest.mark.parametrize(
    "args",
    [
        ("--set", "env.existingSecret=my-env-secret"),
        ("--set", "env.secretsProvider.type=externalSecret"),
        ("--set", "env.secretsProvider.type=vaultStaticSecret"),
    ],
)
def test_no_secret_checksum_when_the_chart_does_not_render_the_secret(chart: Path, args: tuple[str, ...]) -> None:
    """The chart can't see those values, so a checksum of env.secrets would roll the pods for nothing."""
    annotations = _pod_annotations(chart, *args)

    assert "checksum/secret" not in annotations
    assert "checksum/config" in annotations
