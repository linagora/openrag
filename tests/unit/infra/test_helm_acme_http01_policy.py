"""cert-manager answers an HTTP-01 challenge from a solver pod it creates in the
release namespace, on 8089, and the request reaches it through the Ingress
controller, from outside the namespace. The default-deny policy admits neither,
so without an opening of its own the challenge times out and the Certificate
never becomes Ready."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CHART_DIR = Path(__file__).resolve().parents[3] / "infra" / "charts" / "openrag-stack"
HELM = os.environ.get("HELM_BIN") or shutil.which("helm")
requires_helm = pytest.mark.skipif(HELM is None, reason="Helm is not installed")

REQUIRED_SECRETS = [
    "--set",
    "env.secrets.AUTH_TOKEN=or-unit-test-token-0123",
    "--set",
    "postgresql.auth.password=unit-test-password-0123",
]
POLICY = "openrag-acme-http01-solver"
SOLVER_LABELS = {"acme.cert-manager.io/http01-solver": "true"}


@pytest.fixture(scope="module")
def chart(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The chart without its sub-charts: they are fetched archives, and none of
    the objects checked here comes from them."""
    chart = tmp_path_factory.mktemp("chart") / "openrag-stack"
    for directory in ("templates", "rules", "dashboards"):
        shutil.copytree(CHART_DIR / directory, chart / directory)
    shutil.copy(CHART_DIR / "values.yaml", chart / "values.yaml")
    meta = yaml.safe_load((CHART_DIR / "Chart.yaml").read_text(encoding="utf-8"))
    meta.pop("dependencies", None)
    (chart / "Chart.yaml").write_text(yaml.safe_dump(meta), encoding="utf-8")
    return chart


def _policies(chart: Path, *args: str) -> dict[str, dict]:
    assert HELM is not None
    result = subprocess.run(
        [HELM, "template", "openrag", str(chart), "--namespace", "rag", *REQUIRED_SECRETS, *args],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    objects = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
    return {obj["metadata"]["name"]: obj for obj in objects if obj["kind"] == "NetworkPolicy"}


@requires_helm
def test_solver_port_is_open_by_default_on_solver_pods_only(chart: Path) -> None:
    """Rendered with the default-deny, whatever the ingress TLS: the certificate
    may come from a Certificate object or another Ingress, and the policy
    selects nothing until cert-manager starts a solver. Any source, because a
    host-network Ingress controller (k3s's Traefik) connects from a node
    address no namespaceSelector matches."""
    policies = _policies(chart)

    assert "openrag-default-deny" in policies
    spec = policies[POLICY]["spec"]
    assert spec["podSelector"] == {"matchLabels": SOLVER_LABELS}
    assert spec["policyTypes"] == ["Ingress"]
    assert spec["ingress"] == [{"ports": [{"port": 8089, "protocol": "TCP"}]}]


@requires_helm
def test_solver_port_stays_off_every_other_pod(chart: Path) -> None:
    """The workaround this replaces, 8089 in externalPorts, opened it on every pod."""
    default_deny = _policies(chart)["openrag-default-deny"]["spec"]
    ports = [port["port"] for rule in default_deny["ingress"] for port in rule.get("ports", [])]
    assert 8089 not in ports


@requires_helm
def test_solver_sources_can_be_narrowed(chart: Path) -> None:
    peers = [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "ingress-nginx"}}}]
    policies = _policies(chart, "--set-json", f"networkPolicy.acmeHttp01.from={json.dumps(peers)}")

    (rule,) = policies[POLICY]["spec"]["ingress"]
    assert rule["from"] == peers
    assert rule["ports"] == [{"port": 8089, "protocol": "TCP"}]


@requires_helm
def test_solver_policy_can_be_turned_off(chart: Path) -> None:
    policies = _policies(chart, "--set", "networkPolicy.acmeHttp01.enabled=false")

    assert POLICY not in policies
    assert "openrag-default-deny" in policies


@requires_helm
def test_no_solver_policy_without_the_default_deny(chart: Path) -> None:
    """Nothing to open a hole in: every pod is already reachable."""
    assert _policies(chart, "--set", "networkPolicy.enabled=false") == {}
