from __future__ import annotations

import pytest
from api.schemas.admin.model_endpoint_schemas import CreateModelEndpointRequest, UpdateModelEndpointRequest
from api.schemas.admin.partition_schemas import CreatePartitionRequest, UpdatePartitionRequest
from api.schemas.admin.preset_schemas import CreatePresetRequest, UpdatePresetRequest
from pydantic import ValidationError


def test_create_model_endpoint_defaults_and_normalizes_endpoint():
    """Model endpoint creation applies defaults and strips trailing slashes."""
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
    """Only supported endpoint types are accepted."""
    with pytest.raises(ValidationError):
        CreateModelEndpointRequest(name="default", model_type=model_type, endpoint="http://host")


@pytest.mark.parametrize("field,value", [("batch_size", 0), ("timeout", 0)])
def test_create_model_endpoint_rejects_non_positive_numbers(field, value):
    """Batch size and timeout must stay positive."""
    payload = {"name": "default", "model_type": "llm", "endpoint": "http://host", field: value}

    with pytest.raises(ValidationError):
        CreateModelEndpointRequest(**payload)


@pytest.mark.parametrize("endpoint", ["/", "///", "   /  "])
def test_create_model_endpoint_rejects_empty_normalized_endpoint(endpoint):
    """Slash-only endpoint values are invalid after normalization."""
    with pytest.raises(ValidationError):
        CreateModelEndpointRequest(name="default", model_type="llm", endpoint=endpoint)


def test_update_model_endpoint_requires_at_least_one_field():
    """Endpoint updates must contain at least one field."""
    with pytest.raises(ValidationError):
        UpdateModelEndpointRequest()


@pytest.mark.parametrize("key", ["max_llm_context_size", "max_output_tokens"])
@pytest.mark.parametrize("bad", [0, -1, 2.5, "4096", True, False])
def test_create_model_endpoint_rejects_bad_llm_token_budget(key, bad):
    """LLM token budgets in extra must be positive ints (bool/float/str rejected).

    Both bools are covered on purpose: ``bool`` is an ``int`` subclass, so
    ``True`` would otherwise slip through the positive-int check as ``1``, and
    ``False`` must be rejected as a *type* error rather than incidentally by
    the ``<= 0`` bound — the explicit ``isinstance(value, bool)`` guard is what
    both cases pin.
    """
    with pytest.raises(ValidationError, match=key):
        CreateModelEndpointRequest(name="default", model_type="llm", endpoint="http://host", extra={key: bad})


@pytest.mark.parametrize("key", ["max_llm_context_size", "max_output_tokens"])
@pytest.mark.parametrize("bad", [0, -1, 2.5, "4096", True, False])
def test_update_model_endpoint_rejects_bad_llm_token_budget(key, bad):
    with pytest.raises(ValidationError, match=key):
        UpdateModelEndpointRequest(extra={key: bad})


def test_create_model_endpoint_accepts_valid_llm_token_budgets():
    request = CreateModelEndpointRequest(
        name="default",
        model_type="llm",
        endpoint="http://host",
        extra={"max_llm_context_size": 32768, "max_output_tokens": 2048, "implementation": "vllm"},
    )
    assert request.extra["max_llm_context_size"] == 32768
    assert request.extra["max_output_tokens"] == 2048


def test_create_preset_accepts_indexation_and_retrieval_configs():
    """Preset creation accepts both supported preset families."""
    indexation = CreatePresetRequest(name="fast", preset_type="indexation", config={"chunk_size": 512})
    retrieval = CreatePresetRequest(name="qa", preset_type="retrieval", config={"top_k": 20})

    assert indexation.config["chunk_size"] == 512
    assert retrieval.config["top_k"] == 20


def test_create_preset_rejects_unknown_type():
    """Preset type must be either indexation or retrieval."""
    with pytest.raises(ValidationError):
        CreatePresetRequest(name="bad", preset_type="generation", config={})


def test_update_preset_requires_at_least_one_field():
    """Preset updates must contain at least one field."""
    with pytest.raises(ValidationError):
        UpdatePresetRequest()


@pytest.mark.parametrize("payload", [{"name": None}, {"config": None}])
def test_update_preset_rejects_explicit_nulls(payload):
    """Preset updates use omission for unchanged fields and reject null."""
    with pytest.raises(ValidationError):
        UpdatePresetRequest(**payload)


def test_create_partition_defaults_to_default_presets():
    """Partition creation defaults to the default embedder and presets."""
    request = CreatePartitionRequest(name="legal")

    assert request.embedder == "default"
    assert request.indexation_preset == "default"
    assert request.retrieval_preset == "default"
    assert request.chat_history_depth == 4


def test_create_partition_rejects_empty_names():
    """Partition names cannot be blank."""
    with pytest.raises(ValidationError):
        CreatePartitionRequest(name=" ", indexation_preset="default", retrieval_preset="default")


def test_update_partition_requires_at_least_one_field():
    """Partition updates must contain at least one field."""
    with pytest.raises(ValidationError):
        UpdatePartitionRequest()


def test_update_partition_rejects_negative_chat_history_depth():
    """Chat history depth cannot be negative."""
    with pytest.raises(ValidationError):
        UpdatePartitionRequest(chat_history_depth=-1)


def test_update_partition_rejects_zero_chat_history_depth():
    """Chat history depth cannot be zero — a value of 1 turn is the floor."""
    with pytest.raises(ValidationError):
        UpdatePartitionRequest(chat_history_depth=0)


def test_create_partition_rejects_zero_chat_history_depth():
    """Chat history depth cannot be zero at creation time either."""
    with pytest.raises(ValidationError):
        CreatePartitionRequest(name="legal", chat_history_depth=0)


@pytest.mark.parametrize(
    "field",
    [
        "description",
        "embedder",
        "indexation_preset",
        "retrieval_preset",
        "chat_history_depth",
    ],
)
def test_update_partition_rejects_explicit_nulls(field):
    """Partition updates reject null for fields that cannot be cleared."""
    with pytest.raises(ValidationError, match=rf"{field} cannot be null"):
        UpdatePartitionRequest(**{field: None})


def test_update_partition_chat_llm_allows_explicit_null_as_reset():
    """Unlike the other fields, chat_llm=null is a real value: reset to default."""
    request = UpdatePartitionRequest(chat_llm=None)
    assert request.chat_llm is None
    assert "chat_llm" in request.model_fields_set


@pytest.mark.parametrize("blank", ["", "   "])
def test_update_partition_chat_llm_normalizes_blank_to_null(blank):
    """A blank chat_llm means the same as null: reset to the default LLM."""
    assert UpdatePartitionRequest(chat_llm=blank).chat_llm is None


def test_update_partition_chat_llm_strips_whitespace():
    assert UpdatePartitionRequest(chat_llm="  mistral  ").chat_llm == "mistral"


def test_create_partition_chat_llm_normalizes_blank_to_null():
    assert CreatePartitionRequest(name="legal", chat_llm=" ").chat_llm is None
