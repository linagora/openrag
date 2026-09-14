"""Static checks on the chart's external secrets wiring (OpenBao / Vault via
External Secrets Operator).

In the `externalSecret` / `vaultStaticSecret` modes the env Secret is written
by an operator, not by the chart — but the bundled bitnami PostgreSQL sub-chart
still needs to *enforce* the same password. If it is left with neither
`auth.password` nor `auth.existingSecret`, bitnami generates a random one at
install time and OpenRAG can never connect. These tests pin the guard and the
shipped overlay that wires it correctly.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
CHART_DIR = ROOT / "infra" / "charts" / "openrag-stack"
TEMPLATES = CHART_DIR / "templates"


def _template(name: str) -> str:
    return (TEMPLATES / name).read_text(encoding="utf-8")


def _yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_secrets_template_fails_closed_when_bundled_postgres_has_no_credential_source() -> None:
    template = _template("secrets-env.yaml")

    assert ".Values.postgresql.enabled" in template
    assert ".Values.postgresql.auth.existingSecret" in template
    assert ".Values.postgresql.auth.password" in template
    # The guard names the fix so an operator doesn't have to read the template.
    assert "postgresql.auth.existingSecret" in template
    assert "secretKeys" in template


def test_openbao_overlay_wires_the_bundled_postgres_to_the_operator_managed_secret() -> None:
    overlay = _yaml(CHART_DIR / "values-openbao.yaml")
    base = _yaml(CHART_DIR / "values.yaml")

    provider = overlay["env"]["secretsProvider"]
    assert provider["type"] == "externalSecret"
    assert provider["externalSecret"]["secretStore"]["kind"] == "ClusterSecretStore"
    assert provider["externalSecret"]["dataFrom"][0]["extract"]["key"]

    # Bundled Postgres reads its password from the very Secret ESO writes,
    # under the OpenRAG env var name, so both sides always agree.
    pg_auth = overlay["postgresql"]["auth"]
    assert pg_auth["existingSecret"] == f"{base['fullnameOverride']}-env-secrets"
    assert pg_auth["secretKeys"]["adminPasswordKey"] == "POSTGRES_PASSWORD"
    assert pg_auth["secretKeys"]["userPasswordKey"] == "POSTGRES_PASSWORD"
    # No literal credential in the overlay.
    assert not pg_auth.get("password")
    assert "secrets" not in overlay.get("env", {}) or not any(overlay["env"]["secrets"].values())


def test_openrag_deployment_exposes_annotations_for_secret_reloaders() -> None:
    """ESO refreshes the Secret in place but restarts nothing; a reloader
    (e.g. stakater/Reloader) needs an annotation on the Deployment itself."""
    template = _template("openrag.yaml")
    values = _yaml(CHART_DIR / "values.yaml")

    assert ".Values.openrag.annotations" in template
    assert values["openrag"].get("annotations") == {}
