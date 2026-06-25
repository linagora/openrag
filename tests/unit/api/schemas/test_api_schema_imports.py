import pytest
from api.schemas.admin.common import MessageResponse, TaskStatusResponse
from api.schemas.admin.tools import ToolInfo
from api.schemas.admin.users import UserCreate, UserPublic, UserUpdate
from api.schemas.admin.workspaces import AddFilesRequest, CreateWorkspaceRequest
from api.schemas.auth.login import CurrentUserResponse, LoginResponse
from api.schemas.user.chat import OpenAIChatCompletionRequest, OpenAICompletionRequest, OpenAIMessage
from api.schemas.user.search import SearchRequest
from pydantic import ValidationError


def test_user_schemas_import_from_api_package():
    created = UserCreate(display_name="Alice", email=None)
    updated = UserUpdate(display_name="Bob")
    public = UserPublic(id=1, display_name="Alice", external_user_id=None, email=None, is_admin=False, created_at=None)

    assert created.email is None
    assert updated.display_name == "Bob"
    assert public.file_quota is None


def test_openai_schemas_preserve_existing_defaults():
    request = OpenAIChatCompletionRequest(messages=[OpenAIMessage(role="user", content="hello")])
    completion = OpenAICompletionRequest(prompt="hello")

    assert request.temperature == 0.3
    assert request.top_p == 1.0
    assert request.stream is False
    assert completion.best_of == 1


def test_chat_request_forwards_response_format():
    """response_format is a declared field and must survive model_dump so the
    router forwards it to the LLM (e.g. for JSON / structured outputs)
    """
    request = OpenAIChatCompletionRequest(
        messages=[OpenAIMessage(role="user", content="hi")],
        response_format={"type": "json_object"},
    )
    dump = request.model_dump(exclude_none=True)

    assert request.response_format == {"type": "json_object"}
    assert dump["response_format"] == {"type": "json_object"}


def test_chat_request_omits_response_format_when_unset():
    """With exclude_none, an unset response_format must NOT be emitted as null.
    Strict downstream providers reject an explicit response_format: null
    """
    request = OpenAIChatCompletionRequest(messages=[OpenAIMessage(role="user", content="hi")])
    dump = request.model_dump(exclude_none=True)

    assert request.response_format is None
    assert "response_format" not in dump


def test_chat_request_logprobs_opt_in_is_boolean_and_forwarded():
    """logprobs is off by default (server) but a client can opt in. For chat
    completions it's a boolean and must reach the LLM as `true`, not coerced int.
    """
    request = OpenAIChatCompletionRequest.model_validate(
        {"messages": [{"role": "user", "content": "hi"}], "logprobs": True, "top_logprobs": 5}
    )
    dump = request.model_dump(exclude_none=True)

    assert dump["logprobs"] is True
    assert dump["top_logprobs"] == 5


def test_chat_request_omits_logprobs_when_unset():
    """An unset logprobs must not be emitted — the server never requests it on the
    client's behalf (sending it unsolicited can break streaming on some backends).
    """
    request = OpenAIChatCompletionRequest(messages=[OpenAIMessage(role="user", content="hi")])
    dump = request.model_dump(exclude_none=True)

    assert request.logprobs is None
    assert "logprobs" not in dump
    assert "top_logprobs" not in dump


def test_chat_request_passes_through_extra_openai_params():
    """extra='allow' keeps vendor params (tools, seed, ...) that are not declared
    on the model, so any OpenAI-compatible field reaches the downstream LLM
    """
    request = OpenAIChatCompletionRequest.model_validate(
        {
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "function", "function": {"name": "f"}}],
            "seed": 42,
        }
    )
    dump = request.model_dump(exclude_none=True)

    assert dump["tools"] == [{"type": "function", "function": {"name": "f"}}]
    assert dump["seed"] == 42


def test_completion_request_omits_unset_nulls():
    """The /completions router dumps with exclude_none=True (matching chat), so
    optional params left unset are not sent as explicit null to strict providers
    """
    request = OpenAICompletionRequest(prompt="hi")
    dump = request.model_dump(exclude_none=True)

    for unset in ("seed", "stop", "logit_bias", "logprobs"):
        assert unset not in dump


def test_search_schema_preserves_existing_defaults():
    request = SearchRequest(query="hello")

    assert request.query == "hello"
    assert request.top_k == 5


def test_admin_common_schema_imports():
    assert MessageResponse(message="ok").message == "ok"
    assert TaskStatusResponse(task_status_url="/task/1").task_status_url == "/task/1"


def test_auth_schema_imports():
    assert LoginResponse(detail="Logged out").detail == "Logged out"
    assert CurrentUserResponse(user_id=1, auth_method="token").auth_method == "token"


def test_workspace_schema_validates_workspace_id():
    req = CreateWorkspaceRequest(workspace_id="my-ws_1")
    assert req.workspace_id == "my-ws_1"
    assert req.display_name is None

    with pytest.raises(ValidationError):
        CreateWorkspaceRequest(workspace_id="bad/slash")


def test_add_files_request_accepts_id_list():
    req = AddFilesRequest(file_ids=["a", "b"])
    assert req.file_ids == ["a", "b"]


def test_tool_info_schema():
    info = ToolInfo(name="extractText", description="Extract text from a file")
    assert info.name == "extractText"
