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


def test_compose_defaults_preserve_existing_host_paths() -> None:
    compose = _load_yaml(COMPOSE_DIR / "docker-compose.yaml")

    openrag_volumes = compose["x-openrag"]["volumes"]
    rdb_volumes = compose["services"]["rdb"]["volumes"]
    top_level_volumes = set(compose["volumes"])

    assert "${MILVUS_COMPOSE:-milvus/milvus.yaml}" in compose["include"]
    assert "${DATA_VOLUME:-../../data}:/app/data" in openrag_volumes
    assert "${LOG_VOLUME:-../../logs}:/app/logs" in openrag_volumes
    assert "${MODEL_WEIGHTS_VOLUME:-~/.cache/huggingface}:/app/model_weights" in openrag_volumes
    # N8: the ../../openrag source bind-mount is commented out by default
    # (dev-only) so production never lets host changes override the running code.
    assert "../../openrag:/app/openrag" not in openrag_volumes
    assert "${DB_VOLUME:-../../db}:/var/lib/postgresql/data" in rdb_volumes

    assert {"appdata", "logs", "modelweights", "pgdata"} <= top_level_volumes
    assert "${DATA_VOLUME:-appdata}:/app/data" not in openrag_volumes
    assert "${DB_VOLUME:-pgdata}:/var/lib/postgresql/data" not in rdb_volumes


def test_milvus_compose_defaults_preserve_existing_host_paths() -> None:
    compose = _load_yaml(COMPOSE_DIR / "milvus" / "milvus.yaml")

    assert compose["services"]["etcd"]["volumes"] == ["${MILVUS_VOLUME_DIRECTORY:-./volumes}/etcd:/etcd"]
    assert compose["services"]["minio"]["volumes"] == ["${MILVUS_VOLUME_DIRECTORY:-./volumes}/minio:/minio_data"]
    assert compose["services"]["milvus"]["volumes"] == ["${MILVUS_VOLUME_DIRECTORY:-./volumes}/milvus:/var/lib/milvus"]


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
    assert named_milvus["services"]["milvus"]["volumes"] == ["${MILVUS_VOLUME:-milvus}:/var/lib/milvus"]
    assert {"etcd", "minio", "milvus"} <= set(named_milvus["volumes"])

    minio_env = named_milvus["services"]["minio"]["environment"]
    milvus_env = named_milvus["services"]["milvus"]["environment"]
    assert "MINIO_ROOT_USER" not in minio_env
    assert "MINIO_ROOT_PASSWORD" not in minio_env
    assert minio_env["MINIO_ACCESS_KEY"] == "${MINIO_ACCESS_KEY:?Set MINIO_ACCESS_KEY in your .env}"
    assert minio_env["MINIO_SECRET_KEY"] == "${MINIO_SECRET_KEY:?Set MINIO_SECRET_KEY in your .env}"
    assert "minioadmin" not in str(minio_env)
    assert milvus_env["MINIO_ACCESS_KEY_ID"] == "${MINIO_ACCESS_KEY:?Set MINIO_ACCESS_KEY in your .env}"
    assert milvus_env["MINIO_SECRET_ACCESS_KEY"] == "${MINIO_SECRET_KEY:?Set MINIO_SECRET_KEY in your .env}"


def test_quick_start_milvus_uses_matching_minio_credentials() -> None:
    # The quickstart docs tell users to ``cp .env.example .env`` (which defines
    # MINIO_ACCESS_KEY / MINIO_SECRET_KEY) and drop it in quick_start/. The
    # quick_start compose must therefore read the same variable names, both so
    # interpolation succeeds and so Milvus's object-storage creds match minio's.
    quickstart = _load_yaml(ROOT / "infra" / "quick_start" / "vdb" / "milvus.yaml")
    default_env_values = _load_env_example(COMPOSE_DIR / ".env.example")

    minio_env = quickstart["services"]["minio"]["environment"]
    milvus_env = quickstart["services"]["milvus"]["environment"]

    assert "MINIO_ROOT_USER" not in minio_env
    assert "MINIO_ROOT_PASSWORD" not in minio_env
    assert "MINIO_ACCESS_KEY" in default_env_values
    assert "MINIO_SECRET_KEY" in default_env_values
    assert minio_env["MINIO_ACCESS_KEY"] == "${MINIO_ACCESS_KEY:?Set MINIO_ACCESS_KEY in your .env}"
    assert minio_env["MINIO_SECRET_KEY"] == "${MINIO_SECRET_KEY:?Set MINIO_SECRET_KEY in your .env}"
    assert milvus_env["MINIO_ACCESS_KEY_ID"] == minio_env["MINIO_ACCESS_KEY"]
    assert milvus_env["MINIO_SECRET_ACCESS_KEY"] == minio_env["MINIO_SECRET_KEY"]


def test_ollama_cpu_milvus_uses_matching_minio_credentials() -> None:
    compose = _load_yaml(ROOT / "docs" / "assets" / "compose_ollama_cpu.yaml")

    minio_env = compose["services"]["minio"]["environment"]
    milvus_env = compose["services"]["milvus"]["environment"]

    assert milvus_env["MINIO_ACCESS_KEY_ID"] == minio_env["MINIO_ACCESS_KEY"]
    assert milvus_env["MINIO_SECRET_ACCESS_KEY"] == minio_env["MINIO_SECRET_KEY"]


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
