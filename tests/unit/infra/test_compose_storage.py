from __future__ import annotations

from pathlib import Path

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

    assert initializer["image"] == milvus["image"] == "milvusdb/milvus:v3.0.0"
    assert initializer["user"] == "0:0"
    assert initializer["environment"]["LD_PRELOAD"] == ""
    assert initializer["entrypoint"] == ["/bin/sh", "-ec"]
    # The initializer exists to chown the data volume, so that mount must match.
    # It deliberately does not carry the milvus service's server-config mount.
    data_mounts = [v for v in milvus["volumes"] if v.endswith(":/var/lib/milvus")]
    assert initializer["volumes"] == data_mounts
    assert initializer["read_only"] is True
    assert initializer["cap_drop"] == ["ALL"]
    assert initializer["cap_add"] == ["CHOWN", "DAC_READ_SEARCH"]

    command = initializer["command"][0]
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
    assert "${LOG_VOLUME:-../../logs}:/app/logs" in openrag_volumes
    assert "${MODEL_WEIGHTS_VOLUME:-~/.cache/huggingface}:/app/model_weights" in openrag_volumes
    assert compose["x-openrag"]["build"]["args"]["APP_UID"] == "${APP_UID:-1000}"
    # N8: the ../../openrag source bind-mount is commented out by default
    # (dev-only) so production never lets host changes override the running code.
    assert "../../openrag:/app/openrag" not in openrag_volumes
    assert "${DB_VOLUME:-../../db}:/var/lib/postgresql/data" in rdb_volumes

    assert {"appdata", "logs", "modelweights", "pgdata"} <= top_level_volumes
    assert "${DATA_VOLUME:-appdata}:/app/data" not in openrag_volumes
    assert "${DB_VOLUME:-pgdata}:/var/lib/postgresql/data" not in rdb_volumes


def test_milvus_compose_defaults_preserve_existing_host_paths() -> None:
    compose = _load_yaml(COMPOSE_DIR / "milvus" / "milvus.yaml")
    services = compose["services"]

    assert services["etcd"]["volumes"] == ["${MILVUS_VOLUME_DIRECTORY:-./volumes}/etcd:/etcd"]
    assert services["minio"]["volumes"] == ["${MILVUS_VOLUME_DIRECTORY:-./volumes}/minio:/minio_data"]
    assert "${MILVUS_VOLUME_DIRECTORY:-./volumes}/milvus:/var/lib/milvus" in services["milvus"]["volumes"]

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
    assert env_values["LOG_VOLUME"] == "logs"
    assert env_values["MODEL_WEIGHTS_VOLUME"] == "modelweights"
    assert env_values["VLLM_CACHE"] == "modelweights"
    assert env_values["DB_VOLUME"] == "pgdata"
    assert env_values["MILVUS_COMPOSE"] == "milvus/milvus.named-volumes.yaml"

    assert named_milvus["services"]["etcd"]["volumes"] == ["${ETCD_VOLUME:-etcd}:/etcd"]
    assert named_milvus["services"]["minio"]["volumes"] == ["${MINIO_VOLUME:-minio}:/minio_data"]
    assert "${MILVUS_VOLUME:-milvus}:/var/lib/milvus" in named_milvus["services"]["milvus"]["volumes"]
    assert named_milvus["services"]["milvus"]["image"] == "milvusdb/milvus:v3.0.0"
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


def test_storage_v3_is_enabled_for_every_milvus_stack() -> None:
    """Storage V3 gates `add_function_field`, which the Milvus migrations rely on.

    It has to be on wherever Milvus runs — including CI, so migrations are
    exercised on the same storage engine production uses.
    """
    config = _load_yaml(COMPOSE_DIR / "milvus" / "user.yaml")

    assert config["common"]["storage"]["useLoonFFI"] is True
    # Both compaction settings are required for the function-field backfill.
    assert config["dataCoord"]["compaction"]["bumpSchemaVersion"]["enabled"] is True
    assert config["dataCoord"]["compaction"]["storageVersion"]["enabled"] is True

    stacks = {
        COMPOSE_DIR / "milvus" / "milvus.yaml": "./user.yaml",
        COMPOSE_DIR / "milvus" / "milvus.named-volumes.yaml": "./user.yaml",
        ROOT / "tests" / "integration" / "repos" / "docker-compose.yaml": "../../../infra/compose/milvus/user.yaml",
        ROOT
        / "tests"
        / "integration"
        / "api"
        / "api_run"
        / "docker-compose.yaml": "../../../../infra/compose/milvus/user.yaml",
        ROOT / "tests" / "load" / "workspace" / "docker-compose.yml": "../../../infra/compose/milvus/user.yaml",
    }
    for path, source in stacks.items():
        volumes = _load_yaml(path)["services"]["milvus"]["volumes"]
        assert f"{source}:/milvus/configs/user.yaml:ro" in volumes, path


def test_helm_milvus_config_matches_the_compose_one() -> None:
    """Helm cannot read a file outside the chart, so the content is duplicated.

    Compare the parsed documents rather than the text: the two must not drift.
    """
    compose_config = _load_yaml(COMPOSE_DIR / "milvus" / "user.yaml")
    values = _load_yaml(CHART_DIR / "values.yaml")

    assert yaml.safe_load(values["milvus"]["extraConfigFiles"]["user.yaml"]) == compose_config
