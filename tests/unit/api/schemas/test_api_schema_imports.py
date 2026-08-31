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


def test_chat_request_drops_top_logprobs_when_logprobs_not_enabled():
    """top_logprobs only applies when logprobs is enabled. Mirroring OpenAI, an
    unsolicited top_logprobs (logprobs false/omitted) is silently dropped — not
    rejected — so it never reaches strict downstream providers that would error.
    """
    omitted = OpenAIChatCompletionRequest.model_validate(
        {"messages": [{"role": "user", "content": "hi"}], "top_logprobs": 5}
    )
    explicit_false = OpenAIChatCompletionRequest.model_validate(
        {"messages": [{"role": "user", "content": "hi"}], "logprobs": False, "top_logprobs": 5}
    )

    for request in (omitted, explicit_false):
        dump = request.model_dump(exclude_none=True)
        assert request.top_logprobs is None
        assert "top_logprobs" not in dump


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


def test_chat_message_passes_through_extra_openai_fields():
    """An OpenAI message is more than role/content: `name` disambiguates speakers
    and `tool_calls`/`tool_call_id` carry function calling. Dropping them here
    silently truncated the history sent to the LLM
    """
    request = OpenAIChatCompletionRequest.model_validate(
        {
            "messages": [
                {"role": "user", "content": "hi", "name": "alice"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}],
                },
            ]
        }
    )
    messages = request.model_dump(exclude_none=True)["messages"]

    assert messages[0]["name"] == "alice"
    assert messages[1]["tool_calls"][0]["id"] == "c1"


def test_sanitize_messages_keeps_tool_calls_reaching_it():
    """_sanitize_messages leaves a content-free assistant turn alone when it
    carries tool_calls — reachable only now that the schema forwards the field
    """
    from services.orchestrators.query_service import QueryService

    request = OpenAIChatCompletionRequest.model_validate(
        {
            "messages": [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}],
                }
            ]
        }
    )
    sanitized = QueryService._sanitize_messages(request.model_dump(exclude_none=True)["messages"])

    assert sanitized[0]["content"] == ""


def test_completion_request_passes_through_extra_openai_params():
    """Legacy /completions mirrors the chat request: undeclared vendor params are
    forwarded, while the declared bounds on n/best_of still apply
    """
    request = OpenAICompletionRequest.model_validate({"prompt": "hi", "suffix": "!", "user": "alice"})
    dump = request.model_dump(exclude_none=True)

    assert dump["suffix"] == "!"
    assert dump["user"] == "alice"
    with pytest.raises(ValidationError):
        OpenAICompletionRequest.model_validate({"prompt": "hi", "n": 9, "user": "alice"})


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


def test_completion_request_bounds_n_and_best_of():
    # M12: n / best_of each multiply generation cost — reject out-of-range values.
    assert OpenAICompletionRequest(prompt="x", n=8, best_of=8).n == 8
    for bad in ({"n": 0}, {"n": 9}, {"best_of": 0}, {"best_of": 9}):
        with pytest.raises(ValidationError):
            OpenAICompletionRequest(prompt="x", **bad)


def test_chat_message_accepts_tool_role_with_tool_call_id():
    """A tool-result turn is `role="tool"` + `tool_call_id`. `extra="allow"` only
    preserves undeclared fields *after* the declared ones validate, so an
    unlisted role rejected the whole message before its extras mattered
    """
    message = OpenAIMessage.model_validate({"role": "tool", "content": "42", "tool_call_id": "c1"})
    dump = message.model_dump()

    assert dump["role"] == "tool"
    assert dump["tool_call_id"] == "c1"


def test_chat_message_accepts_null_content_with_tool_calls():
    """The assistant turn that *carries* tool_calls has `content: null` in the
    OpenAI API — the exact shape `_sanitize_messages` documents as legitimately
    content-free. A required `content: str` rejected it before it got there
    """
    message = OpenAIMessage.model_validate(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}],
        }
    )

    assert message.content is None
    assert message.model_dump()["tool_calls"][0]["id"] == "c1"


def test_chat_message_accepts_developer_role():
    """`developer` is OpenAI's replacement for `system` on newer models; rejecting
    it 422s a request the downstream LLM would have accepted
    """
    assert OpenAIMessage.model_validate({"role": "developer", "content": "be terse"}).role == "developer"


def test_chat_request_accepts_replayed_tool_call_history():
    """The realistic end-to-end shape: a client replaying a conversation that
    already used tools, then asking a new question. Every intermediate turn must
    survive parsing for the history reaching the LLM to stay faithful
    """
    request = OpenAIChatCompletionRequest.model_validate(
        {
            "messages": [
                {"role": "user", "content": "weather in Paris?"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "get_weather", "arguments": '{"c":"Paris"}'},
                        }
                    ],
                },
                {"role": "tool", "content": "18C", "tool_call_id": "c1"},
                {"role": "assistant", "content": "It's 18C in Paris."},
                {"role": "user", "content": "and tomorrow?"},
            ]
        }
    )
    messages = request.model_dump(exclude_none=True)["messages"]

    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "assistant", "user"]
    assert messages[1]["tool_calls"][0]["function"]["name"] == "get_weather"
    assert messages[2]["tool_call_id"] == "c1"
    # exclude_none drops the null content rather than forwarding `content: null`
    assert "content" not in messages[1]
