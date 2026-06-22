from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
COMPOSE_DIR = ROOT / "infra" / "compose"
CHART_DIR = ROOT / "infra" / "charts" / "openrag-stack"
EXTERN_DIR = ROOT / "extern"


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_compose_defaults_stateful_paths_to_named_volumes() -> None:
    compose = _load_yaml(COMPOSE_DIR / "docker-compose.yaml")

    openrag_volumes = compose["x-openrag"]["volumes"]
    rdb_volumes = compose["services"]["rdb"]["volumes"]
    top_level_volumes = set(compose["volumes"])

    assert "${DATA_VOLUME:-appdata}:/app/data" in openrag_volumes
    assert "${LOG_VOLUME:-logs}:/app/logs" in openrag_volumes
    assert "${MODEL_WEIGHTS_VOLUME:-modelweights}:/app/model_weights" in openrag_volumes
    assert "${DB_VOLUME:-pgdata}:/var/lib/postgresql/data" in rdb_volumes

    assert {"appdata", "logs", "modelweights", "pgdata"} <= top_level_volumes
    assert "../../openrag:/app/openrag" not in openrag_volumes
    assert not any(
        "../../data" in volume or "../../db" in volume or "../../logs" in volume for volume in openrag_volumes
    )
    assert not any("../../db" in volume for volume in rdb_volumes)


def test_milvus_compose_defaults_stateful_paths_to_named_volumes() -> None:
    compose = _load_yaml(COMPOSE_DIR / "milvus" / "milvus.yaml")

    assert compose["services"]["etcd"]["volumes"] == ["${ETCD_VOLUME:-etcd}:/etcd"]
    assert compose["services"]["minio"]["volumes"] == ["${MINIO_VOLUME:-minio}:/minio_data"]
    assert compose["services"]["milvus"]["volumes"] == ["${MILVUS_VOLUME:-milvus}:/var/lib/milvus"]
    assert {"etcd", "minio", "milvus"} <= set(compose["volumes"])


def test_model_serving_cache_defaults_to_named_volume() -> None:
    compose = _load_yaml(COMPOSE_DIR / "docker-compose.yaml")
    infinity = _load_yaml(EXTERN_DIR / "reranker" / "infinity.yaml")
    openai_reranker = _load_yaml(EXTERN_DIR / "reranker" / "openai.yaml")
    transcriber = _load_yaml(EXTERN_DIR / "transcriber.yaml")

    assert compose["x-vllm"]["volumes"] == ["${VLLM_CACHE:-modelweights}:/root/.cache/huggingface"]
    assert infinity["x-reranker"]["volumes"] == ["${VLLM_CACHE:-modelweights}:/app/.cache/huggingface"]
    assert openai_reranker["x-reranker"]["volumes"] == ["${VLLM_CACHE:-modelweights}:/root/.cache/huggingface"]
    assert transcriber["services"]["transcriber"]["volumes"] == ["${VLLM_CACHE:-modelweights}:/root/.cache/huggingface"]


def test_dev_source_bind_mount_lives_in_dev_override() -> None:
    dev_override = _load_yaml(COMPOSE_DIR / "docker-compose.dev.yaml")

    assert "../../openrag:/app/openrag" in dev_override["services"]["openrag"]["volumes"]
    assert "../../openrag:/app/openrag" in dev_override["services"]["openrag-cpu"]["volumes"]


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
