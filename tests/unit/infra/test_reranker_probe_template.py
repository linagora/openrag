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

DEFAULT_MODEL = "Alibaba-NLP/gte-multilingual-reranker-base"
# Infinity 0.0.77 on CPU, cached model, measured to the first /health answer.
SLOWEST_MEASURED_START_SECONDS = 378
# The reranker block as values files copied from chart 0.7.1 or earlier carry
# it: the keys this chart removed, at the values they had.
COPIED_LEGACY_VALUES = f"""
reranker:
  rerankerModelName: {DEFAULT_MODEL}
  servicePort: 7997
  command: [v2, --model-id, {DEFAULT_MODEL}, --port, 7997]
env:
  config:
    RERANKER_MODEL: '{{{{ .Values.reranker.rerankerModelName }}}}'
    RERANKER_BASE_URL: 'http://{{{{ include "openrag-stack.fullname" . }}}}-reranker:{{{{ .Values.reranker.servicePort }}}}'
"""


def _isolated_chart(tmp_path: Path) -> Path:
    """Copy only the parent-chart files needed to render the reranker and its env."""
    chart = tmp_path / "openrag-stack"
    templates = chart / "templates"
    templates.mkdir(parents=True, exist_ok=True)
    shutil.copy(CHART_DIR / "values.yaml", chart / "values.yaml")
    for name in ("_helpers.tpl", "infinity.yaml", "configmap-env.yaml"):
        shutil.copy(CHART_DIR / "templates" / name, templates / name)
    (chart / "Chart.yaml").write_text(
        "apiVersion: v2\nname: openrag-stack\nversion: 0.0.0\n",
        encoding="utf-8",
    )
    return chart


def _render(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    assert HELM is not None
    return subprocess.run(
        [HELM, "template", "test", str(_isolated_chart(tmp_path)), *args],
        check=False,
        capture_output=True,
        text=True,
    )


def _documents(tmp_path: Path, *args: str) -> list[dict]:
    result = _render(tmp_path, *args)
    assert result.returncode == 0, result.stderr
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def _reranker_container(documents: list[dict]) -> dict:
    deployment = next(d for d in documents if d["kind"] == "Deployment" and d["metadata"]["name"].endswith("-reranker"))
    return deployment["spec"]["template"]["spec"]["containers"][0]


def _env_config(documents: list[dict]) -> dict:
    return next(d for d in documents if d["kind"] == "ConfigMap" and d["metadata"]["name"].endswith("-env"))["data"]


def _budget_seconds(probe: dict) -> int:
    return probe.get("initialDelaySeconds", 0) + probe.get("periodSeconds", 10) * probe.get("failureThreshold", 3)


# Probes

HEALTH = {"path": "/health", "port": "reranker"}
DEFAULT_PROBES = {
    "startup": {"httpGet": HEALTH, "periodSeconds": 10, "failureThreshold": 60},
    "readiness": {"httpGet": HEALTH, "periodSeconds": 10, "failureThreshold": 6},
    "liveness": {"httpGet": HEALTH, "initialDelaySeconds": 60, "periodSeconds": 30, "failureThreshold": 3},
}


def _probes(container: dict) -> dict:
    return {
        "startup": container.get("startupProbe"),
        "readiness": container.get("readinessProbe"),
        "liveness": container.get("livenessProbe"),
    }


def _render_without_default_probes(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Render as `helm upgrade --reuse-values` from an earlier chart does: with
    that chart's defaults, which have no probes block."""
    chart = _isolated_chart(tmp_path)
    values = yaml.safe_load((chart / "values.yaml").read_text(encoding="utf-8"))
    del values["reranker"]["probes"]
    (chart / "values.yaml").write_text(yaml.safe_dump(values), encoding="utf-8")
    assert HELM is not None
    return subprocess.run([HELM, "template", "test", str(chart), *args], check=False, capture_output=True, text=True)


def _container_without_default_probes(tmp_path: Path, *args: str) -> dict:
    result = _render_without_default_probes(tmp_path, *args)
    assert result.returncode == 0, result.stderr
    return _reranker_container([document for document in yaml.safe_load_all(result.stdout) if document])


def _containers(tmp_path: Path, *args: str) -> list[dict]:
    """The reranker container with this chart's values and, as after
    `helm upgrade --reuse-values` from an earlier chart, without its probes block."""
    return [
        _reranker_container(_documents(tmp_path / "chart", *args)),
        _container_without_default_probes(tmp_path / "reused", *args),
    ]


@requires_helm
def test_the_default_probes_are_rendered_with_or_without_a_probes_block(tmp_path: Path) -> None:
    for container in _containers(tmp_path):
        assert _probes(container) == DEFAULT_PROBES


def test_values_yaml_lists_the_default_probes() -> None:
    """The defaults live in the template; values.yaml documents them in a comment."""
    text = (CHART_DIR / "values.yaml").read_text(encoding="utf-8")
    for name, probe in DEFAULT_PROBES.items():
        timings = ", ".join(f"{key} {value}" for key, value in probe.items() if key != "httpGet")
        assert f"#   {f'{name}:':<10} {timings}\n" in text


@requires_helm
def test_a_slow_start_is_covered_by_the_startup_probe(tmp_path: Path) -> None:
    """Liveness used to kill Infinity before its warmup ended; it now waits for the startup probe."""
    container = _reranker_container(_documents(tmp_path))

    assert _budget_seconds(container["startupProbe"]) > SLOWEST_MEASURED_START_SECONDS


@requires_helm
def test_readiness_starts_once_the_startup_probe_passes(tmp_path: Path) -> None:
    """An initial delay counts from the container's start, so it would hold a fast start back."""
    container = _reranker_container(_documents(tmp_path))

    assert "initialDelaySeconds" not in container["readinessProbe"]


@requires_helm
def test_a_field_set_alone_keeps_the_rest_of_every_probe(tmp_path: Path) -> None:
    """--reuse-values from an earlier chart has no probes block: a field set
    alone must not leave the probe without its handler and timings."""
    for container in _containers(tmp_path, "--set", "reranker.probes.startup.failureThreshold=120"):
        assert _probes(container) == {
            **DEFAULT_PROBES,
            "startup": {**DEFAULT_PROBES["startup"], "failureThreshold": 120},
        }


@requires_helm
def test_a_handler_set_alone_keeps_the_timings(tmp_path: Path) -> None:
    args = (
        "--set",
        "reranker.probes.startup.httpGet.path=/health",
        "--set",
        "reranker.probes.startup.httpGet.port=reranker",
    )
    for container in _containers(tmp_path, *args):
        assert _probes(container) == DEFAULT_PROBES


@requires_helm
def test_a_handler_field_set_alone_keeps_the_rest_of_the_handler(tmp_path: Path) -> None:
    for container in _containers(tmp_path, "--set", "reranker.probes.liveness.httpGet.path=/ready"):
        assert container["livenessProbe"]["httpGet"] == {"path": "/ready", "port": "reranker"}


@requires_helm
def test_probe_values_are_rendered(tmp_path: Path) -> None:
    container = _reranker_container(
        _documents(
            tmp_path,
            "--set",
            "reranker.probes.startup.failureThreshold=120",
            "--set",
            "reranker.probes.readiness.timeoutSeconds=5",
            "--set",
            "reranker.probes.liveness.periodSeconds=15",
        )
    )

    assert container["startupProbe"]["failureThreshold"] == 120
    assert container["readinessProbe"]["timeoutSeconds"] == 5
    assert container["livenessProbe"]["periodSeconds"] == 15
    assert container["livenessProbe"]["httpGet"] == HEALTH


@requires_helm
def test_a_null_probe_is_dropped(tmp_path: Path) -> None:
    for container in _containers(tmp_path, "--set", "reranker.probes.startup=null"):
        assert _probes(container) == {**DEFAULT_PROBES, "startup": None}


@requires_helm
def test_a_null_probe_is_dropped_from_a_values_file(tmp_path: Path) -> None:
    values = tmp_path / "probes.yaml"
    values.write_text("reranker:\n  probes:\n    startup: null\n", encoding="utf-8")

    container = _reranker_container(_documents(tmp_path, "-f", str(values)))

    assert _probes(container) == {**DEFAULT_PROBES, "startup": None}


@requires_helm
def test_a_null_field_is_removed(tmp_path: Path) -> None:
    for container in _containers(tmp_path, "--set", "reranker.probes.liveness.initialDelaySeconds=null"):
        assert container["livenessProbe"] == {"httpGet": HEALTH, "periodSeconds": 30, "failureThreshold": 3}


@requires_helm
def test_another_handler_replaces_http_get(tmp_path: Path) -> None:
    for args in (
        ("--set", "reranker.probes.liveness.tcpSocket.port=reranker"),
        ("--set", "reranker.probes.liveness.httpGet=null", "--set", "reranker.probes.liveness.tcpSocket.port=reranker"),
    ):
        for container in _containers(tmp_path / str(len(args)), *args):
            assert container["livenessProbe"] == {
                "tcpSocket": {"port": "reranker"},
                "initialDelaySeconds": 60,
                "periodSeconds": 30,
                "failureThreshold": 3,
            }


@requires_helm
def test_a_probe_without_a_handler_is_refused(tmp_path: Path) -> None:
    message = (
        "reranker.probes.liveness has no handler: set httpGet, tcpSocket, exec or grpc, "
        "or set the probe to null to drop it."
    )
    args = ("--set", "reranker.probes.liveness.httpGet=null")

    for result in (_render(tmp_path / "chart", *args), _render_without_default_probes(tmp_path / "reused", *args)):
        assert result.returncode != 0
        assert message in result.stderr


@requires_helm
def test_every_probe_set_to_null_renders_without_probes(tmp_path: Path) -> None:
    args = ("--set", "reranker.probes.startup=null", "--set", "reranker.probes.readiness=null")
    for container in _containers(tmp_path, *args, "--set", "reranker.probes.liveness=null"):
        assert not {"startupProbe", "readinessProbe", "livenessProbe"} & container.keys()


@requires_helm
def test_a_null_probes_block_keeps_the_default_probes(tmp_path: Path) -> None:
    for container in _containers(tmp_path, "--set", "reranker.probes=null"):
        assert _probes(container) == DEFAULT_PROBES


# One source for the model and the port


@requires_helm
def test_model_id_is_the_model_infinity_serves_and_openrag_names(tmp_path: Path) -> None:
    documents = _documents(tmp_path, "--set", "reranker.model.id=BAAI/bge-reranker-v2-m3")

    assert _reranker_container(documents)["args"][:3] == ["v2", "--model-id", "BAAI/bge-reranker-v2-m3"]
    assert _env_config(documents)["RERANKER_MODEL"] == "BAAI/bge-reranker-v2-m3"


@requires_helm
def test_service_port_is_the_port_infinity_listens_on_and_openrag_calls(tmp_path: Path) -> None:
    documents = _documents(tmp_path, "--set", "reranker.service.port=8000")

    container = _reranker_container(documents)
    assert container["args"][3:5] == ["--port", "8000"]
    assert container["ports"] == [{"containerPort": 8000, "name": "reranker"}]
    service = next(d for d in documents if d["kind"] == "Service" and d["metadata"]["name"].endswith("-reranker"))
    assert service["spec"]["ports"][0]["port"] == 8000
    assert _env_config(documents)["RERANKER_BASE_URL"] == "http://openrag-reranker:8000"


@requires_helm
def test_a_values_file_copied_from_the_previous_chart_still_renders(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy.yaml"
    legacy.write_text(COPIED_LEGACY_VALUES, encoding="utf-8")

    documents = _documents(tmp_path, "-f", str(legacy))

    assert _reranker_container(documents)["args"] == ["v2", "--model-id", DEFAULT_MODEL, "--port", "7997"]
    assert _env_config(documents)["RERANKER_MODEL"] == DEFAULT_MODEL
    assert _env_config(documents)["RERANKER_BASE_URL"] == "http://openrag-reranker:7997"


@requires_helm
@pytest.mark.parametrize(
    ("override", "message"),
    [
        ("reranker.rerankerModelName=BAAI/bge-reranker-v2-m3", "reranker.rerankerModelName is"),
        ("reranker.model.id=BAAI/bge-reranker-v2-m3", "reranker.rerankerModelName is"),
        ("reranker.servicePort=8000", "reranker.servicePort is"),
        ("reranker.service.port=8000", "reranker.servicePort is"),
    ],
)
def test_a_copied_values_file_changing_one_of_two_keys_is_refused(tmp_path: Path, override: str, message: str) -> None:
    """Before, these rendered: the model change reranked with another model than
    the one OpenRAG named, the port change left OpenRAG calling a closed port."""
    legacy = tmp_path / "legacy.yaml"
    legacy.write_text(COPIED_LEGACY_VALUES, encoding="utf-8")

    result = _render(tmp_path, "-f", str(legacy), "--set", override)

    assert result.returncode != 0
    assert message in result.stderr


@requires_helm
@pytest.mark.parametrize(
    ("args", "message"),
    [
        pytest.param(
            ("--set", "reranker.rerankerModelName=BAAI/bge-reranker-v2-m3"),
            "reranker.rerankerModelName is",
            id="old-model-key-alone",
        ),
        pytest.param(("--set", "reranker.servicePort=8000"), "reranker.servicePort is", id="old-port-key-alone"),
        # rerankerModelName was the model OpenRAG asked an external reranker for.
        pytest.param(
            (
                "--set",
                "reranker.enabled=false",
                "--set",
                "reranker.externalUrl=http://reranker.example:8000",
                "--set",
                "reranker.rerankerModelName=BAAI/bge-reranker-v2-m3",
            ),
            "reranker.rerankerModelName is",
            id="old-model-key-for-an-external-reranker",
        ),
        pytest.param(
            ("--set-string", "env.config.RERANKER_MODEL=BAAI/bge-reranker-v2-m3"),
            "env.config.RERANKER_MODEL renders",
            id="env-model-unlike-model-id",
        ),
    ],
)
def test_a_removed_key_or_env_value_unlike_its_replacement_is_refused(
    tmp_path: Path, args: tuple[str, ...], message: str
) -> None:
    """A removed key set alone would otherwise be ignored without a word."""
    result = _render(tmp_path, *args)

    assert result.returncode != 0
    assert message in result.stderr


# The env.config block a values file copied from chart 0.7.1 or earlier
# carries, once the removed keys themselves have been deleted.
COPIED_LEGACY_ENV = """
env:
  config:
    RERANKER_MODEL: '{{ .Values.reranker.rerankerModelName }}'
    RERANKER_BASE_URL: '{{ if .Values.reranker.externalUrl }}{{ .Values.reranker.externalUrl }}{{ else }}http://{{ include "openrag-stack.fullname" . }}-reranker:{{ .Values.reranker.servicePort }}{{ end }}'
"""


@requires_helm
@pytest.mark.parametrize(
    "args",
    [
        pytest.param((), id="bundled-reranker"),
        # Nothing else checks an external reranker's model: it would be asked for "".
        pytest.param(
            ("--set", "reranker.enabled=false", "--set", "reranker.externalUrl=http://reranker.example:8000"),
            id="external-reranker",
        ),
    ],
)
def test_a_copied_env_reading_a_deleted_key_is_refused(tmp_path: Path, args: tuple[str, ...]) -> None:
    legacy = tmp_path / "legacy-env.yaml"
    legacy.write_text(COPIED_LEGACY_ENV, encoding="utf-8")

    result = _render(tmp_path, "-f", str(legacy), *args)

    assert result.returncode != 0
    assert "env.config.RERANKER_BASE_URL reads reranker.servicePort" in result.stderr


@requires_helm
def test_a_copied_env_reading_the_deleted_model_key_names_the_replacement(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy-env.yaml"
    legacy.write_text(COPIED_LEGACY_ENV, encoding="utf-8")

    result = _render(tmp_path, "-f", str(legacy), "--set", "reranker.servicePort=7997")

    assert result.returncode != 0
    assert (
        "env.config.RERANKER_MODEL reads reranker.rerankerModelName, which this chart removed, "
        "so it would render empty. Set env.config.RERANKER_MODEL to '{{ .Values.reranker.model.id }}'."
    ) in result.stderr


@requires_helm
def test_a_secret_reading_a_deleted_key_is_refused(tmp_path: Path) -> None:
    values = tmp_path / "secret.yaml"
    values.write_text(
        "env:\n  secrets:\n    RERANKER_API_KEY: '{{ .Values.reranker.rerankerModelName }}'\n", encoding="utf-8"
    )

    result = _render(tmp_path, "-f", str(values))

    assert result.returncode != 0
    assert "env.secrets.RERANKER_API_KEY reads reranker.rerankerModelName" in result.stderr


@requires_helm
def test_command_flags_that_never_applied_are_refused(tmp_path: Path) -> None:
    result = _render(
        tmp_path, "--set", f"reranker.command={{v2,--model-id,{DEFAULT_MODEL},--port,7997,--batch-size,8}}"
    )

    assert result.returncode != 0
    assert "reranker.command is not read by the chart" in result.stderr


@requires_helm
@pytest.mark.parametrize(
    ("flags", "message"),
    [
        ("{--port,9000}", "reranker.extraArgs sets --port"),
        ("{--port=9000}", "reranker.extraArgs sets --port"),
        ("{--model-id,BAAI/bge-reranker-v2-m3}", "reranker.extraArgs sets --model-id"),
        ("{--batch-size,8,--model-id=BAAI/bge-reranker-v2-m3}", "reranker.extraArgs sets --model-id"),
    ],
)
def test_extra_args_cannot_set_the_model_or_the_port(tmp_path: Path, flags: str, message: str) -> None:
    """A second --port moves Infinity off the port the probes and OpenRAG use; a
    second --model-id loads another model than the one OpenRAG names."""
    result = _render(tmp_path, "--set", f"reranker.extraArgs={flags}")

    assert result.returncode != 0
    assert message in result.stderr


@requires_helm
def test_extra_args_may_start_like_a_reserved_flag(tmp_path: Path) -> None:
    container = _reranker_container(_documents(tmp_path, "--set", "reranker.extraArgs={--port-forward-ok}"))

    assert container["args"][-1] == "--port-forward-ok"


@requires_helm
def test_a_service_dns_name_with_the_right_port_is_accepted(tmp_path: Path) -> None:
    url = "http://openrag-reranker.openrag.svc.cluster.local:7997"
    documents = _documents(tmp_path, "--set-string", f"env.config.RERANKER_BASE_URL={url}")

    assert _env_config(documents)["RERANKER_BASE_URL"] == url


@requires_helm
def test_a_url_without_a_port_is_checked_against_its_scheme_default(tmp_path: Path) -> None:
    url = "env.config.RERANKER_BASE_URL=http://openrag-reranker"

    assert _render(tmp_path, "--set-string", url).returncode != 0
    assert _render(tmp_path, "--set-string", url, "--set", "reranker.service.port=80").returncode == 0


@requires_helm
def test_an_external_reranker_is_not_checked_against_the_bundled_one(tmp_path: Path) -> None:
    documents = _documents(
        tmp_path,
        "--set",
        "reranker.externalUrl=http://reranker.example:8000",
        "--set-string",
        "env.config.RERANKER_MODEL=BAAI/bge-reranker-v2-m3",
    )

    assert _env_config(documents)["RERANKER_MODEL"] == "BAAI/bge-reranker-v2-m3"


# Flags and environment of its own


@requires_helm
def test_extra_args_and_env_reach_the_reranker(tmp_path: Path) -> None:
    container = _reranker_container(
        _documents(
            tmp_path,
            "--set",
            "reranker.extraArgs={--batch-size,16}",
            "--set",
            "reranker.extraEnv[0].name=INFINITY_MODEL_WARMUP",
            "--set-string",
            "reranker.extraEnv[0].value=false",
        )
    )

    assert container["args"] == ["v2", "--model-id", DEFAULT_MODEL, "--port", "7997", "--batch-size", "16"]
    assert container["env"] == [{"name": "INFINITY_MODEL_WARMUP", "value": "false"}]
    assert container["envFrom"] == [{"configMapRef": {"name": "openrag-env"}}]
