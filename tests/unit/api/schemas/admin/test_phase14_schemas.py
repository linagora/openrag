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


def test_create_stt_endpoint_requires_a_model_and_accepts_language_hint():
    request = CreateModelEndpointRequest(
        name="moss",
        model_type="stt",
        endpoint="http://moss:8000/v1",
        model_name="moss-transcribe-diarize",
        extra={"language": "fr"},
    )
    assert request.extra == {"language": "fr"}

    with pytest.raises(ValidationError, match="model_name is required"):
        CreateModelEndpointRequest(name="moss", model_type="stt", endpoint="http://moss:8000/v1")

    with pytest.raises(ValidationError, match="extra.language"):
        CreateModelEndpointRequest(
            name="moss",
            model_type="stt",
            endpoint="http://moss:8000/v1",
            model_name="moss-transcribe-diarize",
            extra={"language": "   "},
        )


@pytest.mark.parametrize("output_format", ["moss_timestamped", "moss_speaker_aware"])
def test_create_stt_endpoint_accepts_supported_moss_output_formats(output_format):
    request = CreateModelEndpointRequest(
        name="moss",
        model_type="stt",
        endpoint="http://moss:8000/v1",
        model_name="moss-transcribe-diarize",
        extra={"transcript_output_format": output_format},
    )

    assert request.extra == {"transcript_output_format": output_format}


@pytest.mark.parametrize("output_format", ["raw", "moss", "", True])
def test_create_stt_endpoint_rejects_unknown_transcript_output_format(output_format):
    with pytest.raises(ValidationError, match="extra.transcript_output_format"):
        CreateModelEndpointRequest(
            name="moss",
            model_type="stt",
            endpoint="http://moss:8000/v1",
            model_name="moss-transcribe-diarize",
            extra={"transcript_output_format": output_format},
        )


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


_UNSAFE_NAMES = [
    "owner/model",  # splits across the {model_type}/{name} route segment (#768)
    ".",  # RFC 3986 dot-segment: normalizes to the collection route
    "..",  # RFC 3986 dot-segment: normalizes away the model_type segment too
    "-leading-dash",
    "trailing-dash-",
    ".leading-dot",
    "trailing-dot.",
    "_leading_underscore",
    "trailing_underscore_",
    "has space",
    "has%percent",
    "a" * 129,  # over _NAME_MAX_LENGTH
]


@pytest.mark.parametrize("bad_name", _UNSAFE_NAMES)
def test_create_model_endpoint_rejects_unsafe_name(bad_name):
    """Any name outside the URL-path-segment allowlist is rejected, not just '/'."""
    with pytest.raises(ValidationError):
        CreateModelEndpointRequest(name=bad_name, model_type="reranker", endpoint="http://host")


@pytest.mark.parametrize("bad_name", _UNSAFE_NAMES)
def test_update_model_endpoint_rejects_unsafe_name(bad_name):
    """Same allowlist applies to renames via the update schema — kept in sync
    with the create matrix above (a separate, optional-name validator) so a
    regression in one can't go uncovered by the other."""
    with pytest.raises(ValidationError):
        UpdateModelEndpointRequest(name=bad_name)


@pytest.mark.parametrize("good_name", ["default", "gpt-4.1", "jina_v3", "LocalReranker.prod", "a", "a" * 128])
def test_create_model_endpoint_accepts_realistic_names(good_name):
    """Interior '.', '_', '-' stay available for realistic names."""
    request = CreateModelEndpointRequest(name=good_name, model_type="reranker", endpoint="http://host")
    assert request.name == good_name


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
@pytest.mark.parametrize("model_type", ["embedder", "reranker", "vlm"])
def test_create_non_llm_endpoint_keeps_budget_key_names_unreserved(key, bad, model_type):
    """The budget rules apply to LLM endpoints only.

    These two names are meaningful just for LLMs, so enforcing their shape on
    every endpoint type would reserve them globally and reject an embedder /
    reranker / VLM carrying same-named provider metadata — which the admin UI
    now preserves verbatim, so it would be resubmitted on the next save.
    """
    request = CreateModelEndpointRequest(
        name="default", model_type=model_type, endpoint="http://host", extra={key: bad}
    )
    assert request.extra[key] == bad


@pytest.mark.parametrize("key", ["max_llm_context_size", "max_output_tokens"])
@pytest.mark.parametrize("bad", [0, -1, 2.5, "4096", True, False])
def test_update_model_endpoint_defers_budget_validation_to_the_route(key, bad):
    """``UpdateModelEndpointRequest`` deliberately does not validate budgets.

    It carries no ``model_type`` — that is a path parameter — so it cannot tell
    an LLM update from a non-LLM one, and validating here would reserve the key
    names for every type. The route applies the check once it knows the type;
    ``test_phase14_admin_routers`` pins both sides of that.
    """
    assert UpdateModelEndpointRequest(extra={key: bad}).extra == {key: bad}


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


def test_update_partition_rejects_an_asr_prompt_selection():
    with pytest.raises(ValidationError):
        UpdatePartitionRequest(generation_prompt_names={"asr_transcription": "meeting-diarization"})


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
