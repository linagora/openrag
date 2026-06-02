from __future__ import annotations

import pytest
from api.schemas.admin.model_endpoint_schemas import CreateModelEndpointRequest, UpdateModelEndpointRequest
from api.schemas.admin.partition_schemas import CreatePartitionRequest, UpdatePartitionRequest
from api.schemas.admin.preset_schemas import CreatePresetRequest, UpdatePresetRequest
from pydantic import ValidationError


def test_create_model_endpoint_defaults_and_normalizes_endpoint():
    request = CreateModelEndpointRequest(
        name="default",
        model_type="embedder",
        endpoint="http://embedder:8000/v1/",
        model_name="gte",
    )

    assert request.endpoint == "http://embedder:8000/v1"
    assert request.batch_size == 32
    assert request.timeout == 30.0
    assert request.extra == {}
    assert request.is_default is False


@pytest.mark.parametrize("model_type", ["embedding", "chat", "vision", ""])
def test_create_model_endpoint_rejects_unknown_type(model_type):
    with pytest.raises(ValidationError):
        CreateModelEndpointRequest(name="default", model_type=model_type, endpoint="http://host")


@pytest.mark.parametrize("field,value", [("batch_size", 0), ("timeout", 0)])
def test_create_model_endpoint_rejects_non_positive_numbers(field, value):
    payload = {"name": "default", "model_type": "llm", "endpoint": "http://host", field: value}

    with pytest.raises(ValidationError):
        CreateModelEndpointRequest(**payload)


def test_update_model_endpoint_requires_at_least_one_field():
    with pytest.raises(ValidationError):
        UpdateModelEndpointRequest()


def test_create_preset_accepts_indexation_and_retrieval_configs():
    indexation = CreatePresetRequest(name="fast", preset_type="indexation", config={"chunk_size": 512})
    retrieval = CreatePresetRequest(name="qa", preset_type="retrieval", config={"top_k": 20})

    assert indexation.config["chunk_size"] == 512
    assert retrieval.config["top_k"] == 20


def test_create_preset_rejects_unknown_type():
    with pytest.raises(ValidationError):
        CreatePresetRequest(name="bad", preset_type="generation", config={})


def test_update_preset_requires_at_least_one_field():
    with pytest.raises(ValidationError):
        UpdatePresetRequest()


def test_create_partition_defaults_to_default_presets():
    request = CreatePartitionRequest(name="legal")

    assert request.embedder == "default"
    assert request.indexation_preset == "default"
    assert request.retrieval_preset == "default"
    assert request.chat_history_depth == 0


def test_create_partition_rejects_empty_names():
    with pytest.raises(ValidationError):
        CreatePartitionRequest(name=" ", indexation_preset="default", retrieval_preset="default")


def test_update_partition_requires_at_least_one_field():
    with pytest.raises(ValidationError):
        UpdatePartitionRequest()


def test_update_partition_rejects_negative_chat_history_depth():
    with pytest.raises(ValidationError):
        UpdatePartitionRequest(chat_history_depth=-1)
