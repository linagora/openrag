from types import SimpleNamespace

import pytest
from api.dependencies.retrieval_diagnostics import RetrievalDiagnosticsGuard
from api.routers.user.chat import openai_chat_completion, openai_completion
from api.schemas.user.chat import OpenAIChatCompletionRequest, OpenAICompletionRequest
from fastapi import HTTPException


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metadata",
    [
        {"include_retrieval_trace": True},
        {"compare_original_query": True},
        {
            "include_retrieval_trace": True,
            "require_retrieval": True,
            "bypass_query_contextualization": True,
        },
    ],
)
async def test_chat_diagnostics_require_admin_privileges(metadata):
    request = OpenAIChatCompletionRequest(
        model="openrag-legal",
        messages=[{"role": "user", "content": "question"}],
        metadata=metadata,
    )

    with pytest.raises(HTTPException) as error:
        await openai_chat_completion(
            request2=SimpleNamespace(),
            request=request,
            user={"id": 7, "is_admin": False},
            user_partitions=["legal"],
            service=None,
            partition_service=None,
            config=None,
            diagnostics_guard=RetrievalDiagnosticsGuard("2/minute"),
        )

    assert error.value.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metadata",
    [
        {"include_retrieval_trace": True},
        {"compare_original_query": True},
        {"bypass_query_contextualization": True},
    ],
)
@pytest.mark.parametrize("user", [{"id": 7, "is_admin": False}, {"id": 1, "is_admin": True}])
async def test_text_completion_rejects_unsupported_diagnostics(metadata, user):
    request = OpenAICompletionRequest(
        model="openrag-legal",
        prompt="question",
        metadata=metadata,
    )

    with pytest.raises(HTTPException) as error:
        await openai_completion(
            request2=SimpleNamespace(),
            request=request,
            user=user,
            user_partitions=["legal"],
            service=None,
            partition_service=None,
            config=None,
        )

    assert error.value.status_code == 400
