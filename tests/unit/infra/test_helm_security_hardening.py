from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
CHART_DIR = ROOT / "infra" / "charts" / "openrag-stack"
VALUES = CHART_DIR / "values.yaml"
TEMPLATES = CHART_DIR / "templates"


def _values() -> dict:
    return yaml.safe_load(VALUES.read_text(encoding="utf-8"))


def _template(name: str) -> str:
    return (TEMPLATES / name).read_text(encoding="utf-8")


def _all_templates() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in TEMPLATES.glob("*.yaml"))


def test_helm_defaults_do_not_ship_known_placeholder_secrets() -> None:
    values = _values()
    secrets = values["env"]["secrets"]

    assert values["postgresql"]["auth"]["password"] in ("", None)
    assert values["env"]["config"].get("POSTGRES_PASSWORD") is None
    assert secrets["POSTGRES_PASSWORD"] == "{{ .Values.postgresql.auth.password }}"

    for key in (
        "API_KEY",
        "VLM_API_KEY",
        "EMBEDDER_API_KEY",
        "TRANSCRIBER_API_KEY",
        "AUTH_TOKEN",
        "HF_TOKEN",
    ):
        assert secrets[key] in ("", None)


def test_secret_template_fails_on_required_or_placeholder_secrets() -> None:
    template = _template("secrets-env.yaml")

    assert "fail" in template
    assert "requiredSecrets" in template
    assert "hasKey $secrets $requiredKey" in template
    assert "range $requiredKey := $requiredSecrets" in template
    assert "AUTH_TOKEN" in template
    assert "POSTGRES_PASSWORD" in template
    assert "sk-xxxx" in template
    assert "hf_xxxx" in template
    assert "CHANGE_ME_STRONG_PASSWORD" in template
    # "EMPTY" must NOT be a forbidden placeholder: it is the documented value
    # for EMBEDDER_API_KEY/TRANSCRIBER_API_KEY against local OpenAI-compatible
    # servers (see .env.example), so the chart must accept it.
    assert "EMPTY" not in template


def test_chart_workloads_apply_restricted_security_contexts() -> None:
    values = _values()
    security = values["security"]
    raycluster = _template("raycluster.yaml")

    assert security["automountServiceAccountToken"] is False
    assert security["podSecurityContext"]["runAsNonRoot"] is True
    assert security["podSecurityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
    assert security["containerSecurityContext"]["allowPrivilegeEscalation"] is False
    assert security["containerSecurityContext"]["capabilities"]["drop"] == ["ALL"]
    assert values["vllm"]["servingEngineSpec"]["containerSecurityContext"]["runAsNonRoot"] is True

    templates = _all_templates()
    assert templates.count("automountServiceAccountToken:") >= 5
    assert templates.count(".Values.security.podSecurityContext") >= 5
    assert templates.count(".Values.security.containerSecurityContext") >= 7
    assert "ghcr.io/linagora/openrag:dev-latest" not in raycluster
    assert "image: {{ $.Values.ray.image.repository }}:{{ $.Values.ray.image.tag }}" in raycluster


def test_ingress_is_not_exposed_by_default_and_supports_tls() -> None:
    values = _values()
    template = _template("ingress.yaml")

    assert values["ingress"]["enabled"] is False
    assert values["ingress"]["host"] == ""
    assert values["ingress"]["tls"]["enabled"] is False

    assert "required" in template
    assert "ingress.host" in template
    assert "tls:" in template
    assert "secretName:" in template
