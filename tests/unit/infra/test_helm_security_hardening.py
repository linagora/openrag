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
    """Shared `security` values block (Pod Security Standards "restricted"
    baseline) merged into each workload's own podSecurityContext/
    containerSecurityContext via the openrag-stack.mergeSecurityContext
    helper — a component only sets the runAsUser/runAsGroup/fsGroup specific
    to its own Dockerfile (see values.yaml's comments, e.g. openrag's
    OpenShift arbitrary-UID pattern) and can override automountServiceAccountToken
    / containerSecurityContext too, but none currently need to.
    """
    values = _values()
    security = values["security"]
    raycluster = _template("raycluster.yaml")

    assert security["automountServiceAccountToken"] is False
    assert security["podSecurityContext"]["runAsNonRoot"] is True
    assert security["podSecurityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
    assert security["containerSecurityContext"]["allowPrivilegeEscalation"] is False
    assert security["containerSecurityContext"]["capabilities"]["drop"] == ["ALL"]
    assert values["vllm"]["servingEngineSpec"]["containerSecurityContext"]["runAsNonRoot"] is True

    # Simulate the template helper's `merge (deepCopy component) default` —
    # component keys (e.g. runAsUser) win, the rest is inherited from `security`.
    for component in ("openrag", "adminUi", "reranker", "ray"):
        block = values[component]
        effective_pod_ctx = {**security["podSecurityContext"], **block.get("podSecurityContext", {})}
        assert effective_pod_ctx["runAsNonRoot"] is True, component
        assert effective_pod_ctx["seccompProfile"]["type"] == "RuntimeDefault", component
        assert "runAsUser" in effective_pod_ctx, component

    templates = _all_templates()
    assert templates.count("automountServiceAccountToken:") >= 5
    assert templates.count(".Values.security.podSecurityContext") >= 5
    assert templates.count(".Values.security.containerSecurityContext") >= 7
    assert "ghcr.io/linagora/openrag:dev-latest" not in raycluster
    assert "{{ .Values.ray.image.repository }}:{{ .Values.ray.image.tag }}" in raycluster


def test_ray_dashboard_defaults_to_loopback_in_helm_cluster() -> None:
    values = _values()
    raycluster = _template("raycluster.yaml")

    assert values["ray"]["dashboardHost"] == "127.0.0.1"
    assert "--dashboard-host=0.0.0.0" not in raycluster
    assert "--dashboard-host={{ .Values.ray.dashboardHost }}" in raycluster


def test_ingress_is_not_exposed_by_default_and_supports_tls() -> None:
    """No standalone templates/ingress.yaml here — openrag.yaml and
    raycluster.yaml each render their own optional Ingress (adminUi.ingress
    just toggles a path onto openrag's), gated by their own `required` host
    guard so an enabled Ingress can never render with a blank/wildcard host.
    """
    values = _values()
    openrag_template = _template("openrag.yaml")
    raycluster_template = _template("raycluster.yaml")

    assert values["openrag"]["ingress"]["enabled"] is False
    assert values["openrag"]["ingress"]["host"] == ""
    assert values["adminUi"]["ingress"]["enabled"] is False
    assert values["ray"]["ingress"]["enabled"] is False
    assert values["ray"]["ingress"]["host"] == ""

    for template in (openrag_template, raycluster_template):
        assert "required" in template
        assert "ingress.host must be set" in template
        assert "tls:" in template
