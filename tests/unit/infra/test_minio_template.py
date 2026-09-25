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
HELM = os.environ.get("HELM_BIN") or shutil.which("helm")
requires_helm = pytest.mark.skipif(HELM is None, reason="Helm is not installed")

#: Long enough to clear the floor, and not a value this project publishes.
_GENERATED = "b8f3d1a97c4e2065b8f3d1a97c4e2065"
_BASE_ARGS = ("--set", f"env.secrets.AUTH_TOKEN={_GENERATED}", "--set", f"postgresql.auth.password={_GENERATED}")


def _isolated_chart(tmp_path: Path) -> Path:
    """Copy only the parent-chart files needed to render secrets-env.yaml."""
    chart = tmp_path / "openrag-stack"
    templates = chart / "templates"
    templates.mkdir(parents=True)
    shutil.copy(CHART_DIR / "values.yaml", chart / "values.yaml")
    for name in ("_helpers.tpl", "secrets-env.yaml"):
        shutil.copy(CHART_DIR / "templates" / name, templates / name)
    (chart / "Chart.yaml").write_text("apiVersion: v2\nname: openrag-stack\nversion: 0.0.0\n", encoding="utf-8")
    return chart


def _render(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    assert HELM is not None
    return subprocess.run(
        [HELM, "template", "test", str(_isolated_chart(tmp_path)), *_BASE_ARGS, *args],
        check=False,
        capture_output=True,
        text=True,
    )


def _minio_credentials(access_key: str, secret_key: str) -> tuple[str, ...]:
    return ("--set", f"minioCredentials.accessKey={access_key}", "--set", f"minioCredentials.secretKey={secret_key}")


def _values() -> dict:
    return yaml.safe_load((CHART_DIR / "values.yaml").read_text(encoding="utf-8"))


def _minio_secret_name() -> str:
    return _values()["milvus"]["minio"]["existingSecret"]


def _secrets(rendered: str) -> dict[str, dict]:
    return {doc["metadata"]["name"]: doc for doc in yaml.safe_load_all(rendered) if doc and doc.get("kind") == "Secret"}


# ---------------------------------------------------------------------------
# MinIO credentials: never "minioadmin", never in the Milvus ConfigMap
# ---------------------------------------------------------------------------


@requires_helm
@pytest.mark.parametrize("args", [(), ("--set", "env.existingSecret=my-env")], ids=["values", "existing-secret"])
def test_unset_minio_credentials_fail_the_install(tmp_path: Path, args: tuple[str, ...]) -> None:
    result = _render(tmp_path, *args)

    assert result.returncode != 0
    assert "minioCredentials.accessKey must be set" in result.stderr


@requires_helm
@pytest.mark.parametrize(
    ("access_key", "secret_key", "message"),
    [
        ("minioadmin", "minioadmin", "minioCredentials.accessKey is set to a value published"),
        ("MinioAdmin", _GENERATED, "minioCredentials.accessKey is set to a value published"),
        ("openrag", "minioadmin", "minioCredentials.secretKey is set to a value published"),
        ("openrag", "short", "minioCredentials.secretKey is shorter than 12 characters"),
        ("   ", _GENERATED, "minioCredentials.accessKey must be set"),
    ],
)
def test_published_or_weak_minio_credentials_fail_the_install(
    tmp_path: Path, access_key: str, secret_key: str, message: str
) -> None:
    result = _render(tmp_path, *_minio_credentials(access_key, secret_key))

    assert result.returncode != 0
    assert message in result.stderr


@requires_helm
def test_generated_minio_credentials_go_to_the_secret_minio_and_milvus_read(tmp_path: Path) -> None:
    result = _render(tmp_path, *_minio_credentials("openrag", _GENERATED))

    assert result.returncode == 0, result.stderr
    # The key names are the MinIO chart's; the extraEnv entries in values.yaml use the same.
    assert _secrets(result.stdout)[_minio_secret_name()]["stringData"] == {
        "accesskey": "openrag",
        "secretkey": _GENERATED,
    }


@requires_helm
@pytest.mark.parametrize("field", ["accessKey", "secretKey"])
def test_minio_credentials_set_on_the_milvus_chart_fail_the_install(tmp_path: Path, field: str) -> None:
    """The Milvus chart copies these two into its ConfigMap in plain text."""
    result = _render(tmp_path, *_minio_credentials("openrag", _GENERATED), "--set", f"milvus.minio.{field}=openrag")

    assert result.returncode != 0
    assert f"milvus.minio.{field} is copied into the Milvus ConfigMap" in result.stderr


@requires_helm
def test_a_self_managed_minio_secret_needs_no_credentials(tmp_path: Path) -> None:
    result = _render(tmp_path, "--set", "minioCredentials.create=false")

    assert result.returncode == 0, result.stderr
    assert _minio_secret_name() not in _secrets(result.stdout)


@requires_helm
def test_a_milvus_component_left_without_the_credentials_fails_the_install(tmp_path: Path) -> None:
    """Overriding a component's extraEnv replaces the list from values.yaml."""
    override = tmp_path / "override.yaml"
    override.write_text("milvus:\n  queryNode:\n    extraEnv:\n      - {name: GOGC, value: '100'}\n", encoding="utf-8")

    result = _render(tmp_path, "--set", "minioCredentials.create=false", "-f", str(override))

    assert result.returncode != 0
    assert "milvus.queryNode.extraEnv must set MINIO_ACCESS_KEY_ID" in result.stderr


@requires_helm
def test_an_external_woodpecker_service_fails_the_install(tmp_path: Path) -> None:
    """It gets the credentials as plain env values, from milvus.minio.accessKey."""
    result = _render(
        tmp_path, "--set", "minioCredentials.create=false", "--set", "milvus.streaming.woodpecker.embedded=false"
    )

    assert result.returncode != 0
    assert "external woodpecker service" in result.stderr


@requires_helm
@pytest.mark.parametrize(
    "args",
    [("--set", "milvus.enabled=false"), ("--set", "milvus.minio.enabled=false")],
    ids=["no-milvus", "external-s3"],
)
def test_minio_credentials_are_not_required_without_the_bundled_minio(tmp_path: Path, args: tuple[str, ...]) -> None:
    result = _render(tmp_path, *args)

    assert result.returncode == 0, result.stderr
    assert _minio_secret_name() not in _secrets(result.stdout)


# The tests below render the pinned Milvus chart with this chart's values, to
# check what its templates do with them. Its archive is fetched, not committed:
# they run wherever `helm dependency build` has run and skip elsewhere.
def _pinned_milvus_chart() -> Path:
    chart = yaml.safe_load((CHART_DIR / "Chart.yaml").read_text(encoding="utf-8"))
    dependency = next(d for d in chart["dependencies"] if d["name"] == "milvus")
    return CHART_DIR / "charts" / f"milvus-{dependency['version']}.tgz"


MILVUS_CHART = _pinned_milvus_chart()
requires_milvus_chart = pytest.mark.skipif(
    HELM is None or not MILVUS_CHART.exists(),
    reason=f"needs Helm and {MILVUS_CHART.name} (helm dependency build)",
)


def _render_milvus(tmp_path: Path) -> list[dict]:
    assert HELM is not None
    values = tmp_path / "milvus-values.yaml"
    values.write_text(yaml.safe_dump(_values()["milvus"]), encoding="utf-8")
    result = subprocess.run(
        [HELM, "template", "openrag", str(MILVUS_CHART), "-f", str(values)],
        check=True,
        capture_output=True,
        text=True,
    )
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


@requires_milvus_chart
def test_the_milvus_configmap_holds_no_minio_credentials(tmp_path: Path) -> None:
    # default.yaml is the chart's rendered config.tpl; Milvus reads it with user.yaml.
    configmap = next(
        doc for doc in _render_milvus(tmp_path) if doc["kind"] == "ConfigMap" and "default.yaml" in doc.get("data", {})
    )
    minio = yaml.safe_load(configmap["data"]["default.yaml"])["minio"]

    assert not minio["accessKeyID"]
    assert not minio["secretAccessKey"]


@requires_milvus_chart
def test_minio_and_every_milvus_pod_read_the_credentials_secret(tmp_path: Path) -> None:
    secret = _minio_secret_name()
    expected = {
        "milvus": {"MINIO_ACCESS_KEY_ID": (secret, "accesskey"), "MINIO_SECRET_ACCESS_KEY": (secret, "secretkey")},
        "minio": {"MINIO_ACCESS_KEY": (secret, "accesskey"), "MINIO_SECRET_KEY": (secret, "secretkey")},
    }
    repositories = {"milvusdb/milvus": "milvus", _values()["milvus"]["minio"]["image"]["repository"]: "minio"}
    seen = []
    for doc in _render_milvus(tmp_path):
        if doc["kind"] not in ("Deployment", "StatefulSet"):
            continue
        for container in doc["spec"]["template"]["spec"]["containers"]:
            kind = repositories.get(container["image"].split(":")[0])
            if kind is None:
                continue
            refs = {
                env["name"]: (env["valueFrom"]["secretKeyRef"]["name"], env["valueFrom"]["secretKeyRef"]["key"])
                for env in container.get("env", [])
                if "secretKeyRef" in env.get("valueFrom", {})
            }
            assert expected[kind].items() <= refs.items(), doc["metadata"]["name"]
            seen.append(kind)

    assert seen.count("minio") == 1
    assert seen.count("milvus") >= 5


@requires_milvus_chart
def test_the_install_check_covers_every_milvus_component() -> None:
    """A Milvus chart bump that adds a component must add it to the check in
    secrets-env.yaml, and to the extraEnv entries in values.yaml."""
    assert HELM is not None
    chart_values = yaml.safe_load(
        subprocess.run([HELM, "show", "values", str(MILVUS_CHART)], check=True, capture_output=True, text=True).stdout
    )
    # Not Milvus: separate binaries with their own configuration.
    other_binaries = {"cdc", "woodpecker", "tei"}
    components = {name for name, value in chart_values.items() if isinstance(value, dict) and "extraEnv" in value}
    template = (CHART_DIR / "templates" / "secrets-env.yaml").read_text(encoding="utf-8")
    checked = re.search(r"range \$component := list ([^}]*)}}", template)

    assert checked, "component list not found in secrets-env.yaml"
    assert set(re.findall(r'"([^"]+)"', checked.group(1))) == components - other_binaries
    assert all("extraEnv" in _values()["milvus"][name] for name in components - other_binaries)


# ---------------------------------------------------------------------------
# One MinIO image everywhere
# ---------------------------------------------------------------------------


def test_every_stack_runs_the_same_minio_image() -> None:
    """Compose, Helm and the test stacks must agree: data written by a newer
    build can't be read by an older one, so a stack left behind on another tag
    can't share volumes or fixtures with the others."""
    values = yaml.safe_load((CHART_DIR / "values.yaml").read_text(encoding="utf-8"))
    helm_image = values["milvus"]["minio"]["image"]
    expected = f"{helm_image['repository']}:{helm_image['tag']}"

    compose_files = [
        ROOT / "infra/compose/milvus/milvus.yaml",
        ROOT / "infra/compose/milvus/milvus.named-volumes.yaml",
        ROOT / "tests/integration/api/api_run/docker-compose.yaml",
        ROOT / "tests/integration/repos/docker-compose.yaml",
        ROOT / "tests/load/workspace/docker-compose.yml",
    ]
    images = {
        path.relative_to(ROOT).as_posix(): yaml.safe_load(path.read_text(encoding="utf-8"))["services"]["minio"][
            "image"
        ]
        for path in compose_files
    }

    assert images == dict.fromkeys(images, expected)
    # A tag alone can be re-pushed upstream; the digest fixes what runs.
    assert "@sha256:" in expected
