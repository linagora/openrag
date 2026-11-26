from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
COMPOSE_DIR = ROOT / "infra" / "compose"
CHART_DIR = ROOT / "infra" / "charts" / "openrag-stack"
EXTERN_DIR = ROOT / "extern"


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _load_env_example(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def _assert_milvus_initializer(services: dict) -> None:
    """Assert that Milvus starts only after its storage is safe for UID 999."""
    initializer = services["milvus-init"]
    milvus = services["milvus"]

    assert initializer["image"] == milvus["image"] == "milvusdb/milvus:v3.0.5"
    assert initializer["user"] == "0:0"
    assert initializer["environment"]["LD_PRELOAD"] == ""
    assert initializer["entrypoint"] == ["/bin/sh", "-ec"]
    assert initializer["volumes"] == milvus["volumes"]
    assert initializer["read_only"] is True
    assert initializer["cap_drop"] == ["ALL"]
    assert initializer["cap_add"] == ["CHOWN", "DAC_READ_SEARCH"]

    command = initializer["command"][0]
    # Already-initialized storage must not be rescanned (issue #1493).
    assert '"$$(stat -c %u:%g /var/lib/milvus)" = "999:999"' in command
    assert "! -uid 999" in command
    assert "! -gid 999" in command
    assert "exit 1" in command
    assert "chown 999:999 /var/lib/milvus" in command
    assert milvus["depends_on"]["milvus-init"]["condition"] == "service_completed_successfully"


def test_compose_defaults_preserve_existing_host_paths() -> None:
    compose = _load_yaml(COMPOSE_DIR / "docker-compose.yaml")

    openrag_volumes = compose["x-openrag"]["volumes"]
    rdb_volumes = compose["services"]["rdb"]["volumes"]
    top_level_volumes = set(compose["volumes"])

    assert "${MILVUS_COMPOSE:-milvus/milvus.yaml}" in compose["include"]
    assert "${DATA_VOLUME:-../../data}:/app/data" in openrag_volumes
    assert not any("/app/logs" in v for v in openrag_volumes)
    assert "${MODEL_WEIGHTS_VOLUME:-~/.cache/huggingface}:/app/model_weights" in openrag_volumes
    assert compose["x-openrag"]["build"]["args"]["APP_UID"] == "${APP_UID:-1000}"
    # N8: the ../../openrag source bind-mount is commented out by default
    # (dev-only) so production never lets host changes override the running code.
    assert "../../openrag:/app/openrag" not in openrag_volumes
    assert "${DB_VOLUME:-../../db}:/var/lib/postgresql/data" in rdb_volumes

    assert {"appdata", "modelweights", "pgdata"} <= top_level_volumes
    assert "logs" not in top_level_volumes
    assert "${DATA_VOLUME:-appdata}:/app/data" not in openrag_volumes
    assert "${DB_VOLUME:-pgdata}:/var/lib/postgresql/data" not in rdb_volumes


def test_bundled_storage_waits_for_secret_preflight() -> None:
    for path in (COMPOSE_DIR / "milvus" / "milvus.yaml", COMPOSE_DIR / "milvus" / "milvus.named-volumes.yaml"):
        services = _load_yaml(path)["services"]
        check = services["compose-secret-check"]
        assert check["command"] == ["python3", "/workspace/scripts/gen_env.py", "--check-compose-storage"]
        assert check["user"] == "${OPENRAG_PREFLIGHT_UID:-1000}:${OPENRAG_PREFLIGHT_GID:-1000}"
        assert check["network_mode"] == "none"
        assert check["read_only"] is True
        assert check["volumes"] == [
            "../../../scripts/gen_env.py:/workspace/scripts/gen_env.py:ro",
            "../../../openrag/core:/workspace/openrag/core:ro",
            "${OPENRAG_ENV_FILE:-../.env}:/run/openrag/shared.env:ro",
        ]
        assert "MINIO_ACCESS_KEY" in check["environment"]
        assert "MINIO_SECRET_KEY" in check["environment"]
        assert services["minio"]["depends_on"]["compose-secret-check"]["condition"] == "service_completed_successfully"
        assert (
            services["milvus-init"]["depends_on"]["compose-secret-check"]["condition"]
            == "service_completed_successfully"
        )


def test_postgres_waits_for_effective_password_preflight() -> None:
    services = _load_yaml(COMPOSE_DIR / "docker-compose.yaml")["services"]
    check = services["compose-postgres-secret-check"]

    assert check["command"] == ["python3", "/workspace/scripts/gen_env.py", "--check-compose-postgres"]
    assert check["user"] == "${OPENRAG_PREFLIGHT_UID:-1000}:${OPENRAG_PREFLIGHT_GID:-1000}"
    assert check["network_mode"] == "none"
    assert check["read_only"] is True
    assert check["volumes"] == [
        "../../scripts/gen_env.py:/workspace/scripts/gen_env.py:ro",
        "../../openrag/core:/workspace/openrag/core:ro",
        "${OPENRAG_ENV_FILE:-./.env}:/run/openrag/shared.env:ro",
    ]
    assert "POSTGRES_PASSWORD" in check["environment"]
    assert (
        services["rdb"]["depends_on"]["compose-postgres-secret-check"]["condition"] == "service_completed_successfully"
    )


def test_compose_preflights_run_as_the_shared_env_owner() -> None:
    profiles = (
        (COMPOSE_DIR / "docker-compose.yaml", "compose-postgres-secret-check"),
        (COMPOSE_DIR / "milvus" / "milvus.yaml", "compose-secret-check"),
        (COMPOSE_DIR / "milvus" / "milvus.named-volumes.yaml", "compose-secret-check"),
        (COMPOSE_DIR / "monitoring.docker-compose.yaml", "compose-grafana-secret-check"),
    )
    for path, service_name in profiles:
        service = _load_yaml(path)["services"][service_name]
        assert service["user"] == "${OPENRAG_PREFLIGHT_UID:-1000}:${OPENRAG_PREFLIGHT_GID:-1000}"
        assert (
            service["environment"]["OPENRAG_PREFLIGHT_ALLOW_INSECURE_OVERRIDE_SET"] == "${ALLOW_INSECURE_SECRETS+set}"
        )
        assert any("OPENRAG_ENV_FILE" in volume for volume in service["volumes"])


def test_rendered_compose_uses_explicit_env_file_without_exposing_host_path_to_api(tmp_path: Path) -> None:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker Compose is required to render Compose configuration")

    template = tmp_path / ".env.example"
    template_text = (COMPOSE_DIR / ".env.example").read_text(encoding="utf-8")
    template.write_text(template_text, encoding="utf-8")
    env_file = tmp_path / "prod.env"
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "gen_env.py"), "-t", str(template), "-o", str(env_file)],
        capture_output=True,
        check=True,
        text=True,
    )
    env_text = env_file.read_text(encoding="utf-8")
    env_text += f"SHARED_ENV={env_file.resolve()}\n"
    env_file.write_text(env_text, encoding="utf-8")
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "gen_env.py"), "-t", str(template), "-o", str(env_file)],
        capture_output=True,
        check=True,
        text=True,
    )
    env_text = env_file.read_text(encoding="utf-8")
    env_text = env_text.replace(f"OPENRAG_PREFLIGHT_UID={os.geteuid()}", "OPENRAG_PREFLIGHT_UID=1002")
    env_text = env_text.replace(f"OPENRAG_PREFLIGHT_GID={os.getegid()}", "OPENRAG_PREFLIGHT_GID=2002")
    env_file.write_text(env_text, encoding="utf-8")

    for cwd, compose_file, monitoring_file in (
        (COMPOSE_DIR, "docker-compose.yaml", "monitoring.docker-compose.yaml"),
        (ROOT, "infra/compose/docker-compose.yaml", "infra/compose/monitoring.docker-compose.yaml"),
    ):
        env = os.environ.copy()
        env["OPENRAG_ENV_FILE"] = str(env_file.resolve())
        env["PWD"] = str(cwd)
        env["GRAFANA_ADMIN_PASSWORD"] = "or-unit-test-token-0123"
        env["METRICS_TOKEN"] = "or-unit-test-token-4567"
        result = subprocess.run(
            [
                docker,
                "compose",
                "--env-file",
                str(env_file),
                "-f",
                compose_file,
                "-f",
                monitoring_file,
                "config",
                "--format",
                "json",
            ],
            cwd=cwd,
            env=env,
            capture_output=True,
            check=True,
            text=True,
        )
        services = json.loads(result.stdout)["services"]

        for service_name in (
            "compose-postgres-secret-check",
            "compose-secret-check",
            "compose-grafana-secret-check",
        ):
            service = services[service_name]
            assert service["user"] == "1002:2002"
            shared_mount = next(v for v in service["volumes"] if v["target"] == "/run/openrag/shared.env")
            assert Path(shared_mount["source"]).resolve() == env_file.resolve()

        assert "SHARED_ENV" not in services["openrag"]["environment"]
        assert not any(volume["target"] == "/ray_mount/.env" for volume in services["openrag"]["volumes"])
        expected_auth_token = next(
            line.partition("=")[2] for line in env_text.splitlines() if line.startswith("AUTH_TOKEN=")
        )
        expected_postgres_password = next(
            line.partition("=")[2] for line in env_text.splitlines() if line.startswith("POSTGRES_PASSWORD=")
        )
        assert services["openrag"]["environment"]["AUTH_TOKEN"] == expected_auth_token
        assert services["openrag"]["environment"]["POSTGRES_PASSWORD"] == expected_postgres_password
        assert services["rdb"]["environment"]["POSTGRES_PASSWORD"] == expected_postgres_password
        assert services["compose-postgres-secret-check"]["environment"]["POSTGRES_PASSWORD"] == expected_postgres_password


def test_legacy_compose_rollback_uses_env_file_default_after_generator_migration(tmp_path: Path) -> None:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker Compose is required to render Compose configuration")

    template = tmp_path / ".env.example"
    template.write_text(
        "AUTH_TOKEN=__GENERATE_ME__\nOPENRAG_PREFLIGHT_UID=__GENERATE_ME__\nOPENRAG_PREFLIGHT_GID=__GENERATE_ME__\n",
        encoding="utf-8",
    )
    env_file = tmp_path / ".env"
    env_file.write_text(
        "SHARED_ENV=/home/operator/openrag/infra/compose/.env\nAUTH_TOKEN=or-existing-token\n",
        encoding="utf-8",
    )
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "gen_env.py"), "-t", str(template), "-o", str(env_file)],
        capture_output=True,
        check=True,
        text=True,
    )
    legacy_compose = tmp_path / "docker-compose-v2.3.5.yaml"
    legacy_compose.write_text(
        "services:\n"
        "  openrag:\n"
        "    image: linagoraai/openrag:v2.3.5\n"
        "    env_file:\n"
        "      - ${SHARED_ENV:-.env}\n"
        "    volumes:\n"
        "      - ${SHARED_ENV:-./.env}:/ray_mount/.env\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env.pop("SHARED_ENV", None)
    result = subprocess.run(
        [docker, "compose", "-f", str(legacy_compose), "config", "--format", "json"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        check=True,
        text=True,
    )
    service = json.loads(result.stdout)["services"]["openrag"]

    assert "SHARED_ENV" not in service["environment"]
    assert service["environment"]["AUTH_TOKEN"] == "or-existing-token"
    assert Path(service["volumes"][0]["source"]).resolve() == env_file.resolve()


@pytest.mark.parametrize("compose_file", ["milvus/milvus.yaml", "milvus/milvus.named-volumes.yaml"])
def test_milvus_preflight_default_resolves_to_compose_env_file(tmp_path: Path, compose_file: str) -> None:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker Compose is required to render Compose configuration")

    template = COMPOSE_DIR / ".env.example"
    env_file = tmp_path / "compose.env"
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "gen_env.py"), "-t", str(template), "-o", str(env_file)],
        capture_output=True,
        check=True,
        text=True,
    )
    env = os.environ.copy()
    env.pop("OPENRAG_ENV_FILE", None)
    env.pop("SHARED_ENV", None)
    result = subprocess.run(
        [docker, "compose", "--env-file", str(env_file), "-f", compose_file, "config", "--format", "json"],
        cwd=COMPOSE_DIR,
        env=env,
        capture_output=True,
        check=True,
        text=True,
    )
    services = json.loads(result.stdout)["services"]
    shared_mount = next(
        volume
        for volume in services["compose-secret-check"]["volumes"]
        if volume["target"] == "/run/openrag/shared.env"
    )

    assert Path(shared_mount["source"]).resolve() == (COMPOSE_DIR / ".env").resolve()


def test_milvus_compose_defaults_preserve_existing_host_paths() -> None:
    compose = _load_yaml(COMPOSE_DIR / "milvus" / "milvus.yaml")
    services = compose["services"]

    assert services["etcd"]["volumes"] == ["${MILVUS_VOLUME_DIRECTORY:-./volumes}/etcd:/etcd"]
    assert services["minio"]["volumes"] == ["${MILVUS_VOLUME_DIRECTORY:-./volumes}/minio:/minio_data"]
    assert services["milvus"]["volumes"] == ["${MILVUS_VOLUME_DIRECTORY:-./volumes}/milvus:/var/lib/milvus"]

    _assert_milvus_initializer(services)
    assert services["milvus"]["environment"]["ETCD_AUTH_ENABLED"] == "false"
    assert services["milvus"]["environment"]["MQ_TYPE"] == "${MILVUS_MQ_TYPE:-default}"
    assert services["milvus"]["depends_on"]["milvus-init"]["condition"] == "service_completed_successfully"


def test_named_volume_profile_is_opt_in() -> None:
    default_env_values = _load_env_example(COMPOSE_DIR / ".env.example")
    env_values = _load_env_example(COMPOSE_DIR / ".env.named-volumes.example")
    named_milvus = _load_yaml(COMPOSE_DIR / "milvus" / "milvus.named-volumes.yaml")

    assert "MINIO_ACCESS_KEY" in default_env_values
    assert "MINIO_SECRET_KEY" in default_env_values

    assert env_values["DATA_VOLUME"] == "appdata"
    assert "LOG_VOLUME" not in env_values
    assert env_values["MODEL_WEIGHTS_VOLUME"] == "modelweights"
    assert env_values["VLLM_CACHE"] == "modelweights"
    assert env_values["DB_VOLUME"] == "pgdata"
    assert env_values["MILVUS_COMPOSE"] == "milvus/milvus.named-volumes.yaml"

    assert named_milvus["services"]["etcd"]["volumes"] == ["${ETCD_VOLUME:-etcd}:/etcd"]
    assert named_milvus["services"]["minio"]["volumes"] == ["${MINIO_VOLUME:-minio}:/minio_data"]
    assert named_milvus["services"]["milvus"]["volumes"] == ["${MILVUS_VOLUME:-milvus}:/var/lib/milvus"]
    assert named_milvus["services"]["milvus"]["image"] == "milvusdb/milvus:v3.0.5"
    assert named_milvus["services"]["milvus"]["environment"]["ETCD_AUTH_ENABLED"] == "false"
    assert named_milvus["services"]["milvus"]["environment"]["MQ_TYPE"] == "${MILVUS_MQ_TYPE:-default}"
    assert {"etcd", "minio", "milvus"} <= set(named_milvus["volumes"])
    _assert_milvus_initializer(named_milvus["services"])

    minio_env = named_milvus["services"]["minio"]["environment"]
    milvus_env = named_milvus["services"]["milvus"]["environment"]
    assert "MINIO_ROOT_USER" not in minio_env
    assert "MINIO_ROOT_PASSWORD" not in minio_env
    assert minio_env["MINIO_ACCESS_KEY"] == "${MINIO_ACCESS_KEY:?Set MINIO_ACCESS_KEY in your .env}"
    assert minio_env["MINIO_SECRET_KEY"] == "${MINIO_SECRET_KEY:?Set MINIO_SECRET_KEY in your .env}"
    assert "minioadmin" not in str(minio_env)
    assert milvus_env["MINIO_ACCESS_KEY_ID"] == "${MINIO_ACCESS_KEY:?Set MINIO_ACCESS_KEY in your .env}"
    assert milvus_env["MINIO_SECRET_ACCESS_KEY"] == "${MINIO_SECRET_KEY:?Set MINIO_SECRET_KEY in your .env}"


def test_model_serving_cache_preserves_host_path_default_with_named_volume_opt_in() -> None:
    compose = _load_yaml(COMPOSE_DIR / "docker-compose.yaml")
    infinity = _load_yaml(EXTERN_DIR / "reranker" / "infinity.yaml")
    openai_reranker = _load_yaml(EXTERN_DIR / "reranker" / "openai.yaml")
    transcriber = _load_yaml(EXTERN_DIR / "transcriber.yaml")

    assert compose["x-vllm"]["volumes"] == ["${VLLM_CACHE:-/root/.cache/huggingface}:/root/.cache/huggingface"]
    assert infinity["x-reranker"]["volumes"] == ["${VLLM_CACHE:-/root/.cache/huggingface}:/app/.cache/huggingface"]
    assert openai_reranker["x-reranker"]["volumes"] == [
        "${VLLM_CACHE:-/root/.cache/huggingface}:/root/.cache/huggingface"
    ]
    assert transcriber["services"]["transcriber"]["volumes"] == [
        "${VLLM_CACHE:-/root/.cache/huggingface}:/root/.cache/huggingface"
    ]


def test_helm_storage_is_pvc_based_without_host_paths() -> None:
    values = _load_yaml(CHART_DIR / "values.yaml")
    rendered_templates = "\n".join(
        path.read_text(encoding="utf-8") for path in (CHART_DIR / "templates").glob("*.yaml")
    )

    assert "hostPath" not in rendered_templates
    assert values["persistence"]["enabled"] is True
    assert values["postgresql"]["primary"]["persistence"]["enabled"] is True
    assert values["milvus"]["minio"]["persistence"]["enabled"] is True
    assert values["milvus"]["etcd"]["persistence"]["enabled"] is True


def test_minio_image_is_digest_pinned_in_every_stack() -> None:
    values = _load_yaml(CHART_DIR / "values.yaml")
    helm_image = values["milvus"]["minio"]["image"]
    expected = f"{helm_image['repository']}:{helm_image['tag']}"

    assert re.fullmatch(r".+@sha256:[0-9a-f]{64}", expected), expected

    compose_files = (
        COMPOSE_DIR / "milvus" / "milvus.yaml",
        COMPOSE_DIR / "milvus" / "milvus.named-volumes.yaml",
        ROOT / "tests" / "integration" / "api" / "api_run" / "docker-compose.yaml",
        ROOT / "tests" / "integration" / "repos" / "docker-compose.yaml",
        ROOT / "tests" / "load" / "workspace" / "docker-compose.yml",
    )
    for path in compose_files:
        assert _load_yaml(path)["services"]["minio"]["image"] == expected, path


def test_milvus_image_matches_helm_version_in_every_stack() -> None:
    chart = _load_yaml(CHART_DIR / "Chart.yaml")
    lock = _load_yaml(CHART_DIR / "Chart.lock")
    milvus_dependency = next(dependency for dependency in chart["dependencies"] if dependency["name"] == "milvus")
    locked_milvus = next(dependency for dependency in lock["dependencies"] if dependency["name"] == "milvus")

    assert milvus_dependency["version"] == locked_milvus["version"] == "5.0.33"
    values = _load_yaml(CHART_DIR / "values.yaml")
    assert values["milvus"].get("image", {}).get("all", {}).get("tag") is None
    expected = "milvusdb/milvus:v3.0.5"

    compose_files = (
        COMPOSE_DIR / "milvus" / "milvus.yaml",
        COMPOSE_DIR / "milvus" / "milvus.named-volumes.yaml",
        ROOT / "tests" / "integration" / "api" / "api_run" / "docker-compose.yaml",
        ROOT / "tests" / "integration" / "repos" / "docker-compose.yaml",
        ROOT / "tests" / "load" / "workspace" / "docker-compose.yml",
    )
    for path in compose_files:
        services = _load_yaml(path)["services"]
        assert services["milvus"]["image"] == expected, path
        if "milvus-init" in services:
            assert services["milvus-init"]["image"] == expected, path
