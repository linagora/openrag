"""Shaping the model's answer into the response: citations, the empty-answer retry, extra."""

import pytest
from core.chat.answer import (
    build_extra_payload,
    empty_response_fallback,
    empty_response_retry_messages,
    is_structured_output,
    retry_empty_response_stream,
    sampling_params,
)


def test_empty_response_retry_instruction_uses_one_system_message():
    messages = [
        {"role": "system", "content": "Follow the existing answer instructions."},
        {"role": "user", "content": "ok"},
    ]

    retry_messages = empty_response_retry_messages(messages)

    assert [message["role"] for message in retry_messages].count("system") == 1
    assert "Follow the existing answer instructions." in retry_messages[0]["content"]
    assert "previous response was empty" in retry_messages[0]["content"]
    assert messages[0]["content"] == "Follow the existing answer instructions."


def test_sampling_params_strip_transport_keys():
    out = sampling_params({"messages": [], "stream": True, "model": "m", "temperature": 0.5})
    assert out == {"temperature": 0.5}


@pytest.mark.asyncio
@pytest.mark.parametrize("close_early", [False, True])
async def test_empty_response_retry_closes_streams_before_retry_and_on_disconnect(close_early):
    close_order = []
    retry_opened_after = []

    class TrackedStream:
        def __init__(self, name, lines):
            self.name = name
            self.lines = list(lines)

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self.lines:
                raise StopAsyncIteration
            return self.lines.pop(0)

        async def aclose(self):
            close_order.append(self.name)

    initial_stream = TrackedStream(
        "initial",
        [
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
            "data: [DONE]\n\n",
        ],
    )

    def retry_stream_factory():
        retry_opened_after.append(list(close_order))
        return TrackedStream(
            "retry",
            [
                'data: {"choices":[{"delta":{"content":"Answer"}}]}\n\n',
                'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
                "data: [DONE]\n\n",
            ],
        )

    response_stream = retry_empty_response_stream(
        initial_stream,
        retry_stream_factory,
        fallback_text="Please try again.",
        fallback_extra={},
        model_name="m",
    )
    if close_early:
        await anext(response_stream)
        await response_stream.aclose()
    else:
        _ = [line async for line in response_stream]

    assert retry_opened_after == [["initial"]]
    assert close_order == ["initial", "retry"]


def test_empty_response_retry_messages_adds_a_system_message_when_there_is_none():
    retry_messages = empty_response_retry_messages([{"role": "user", "content": "q"}])
    assert [message["role"] for message in retry_messages] == ["system", "user"]


@pytest.mark.parametrize(
    ("latest", "detected", "expected"),
    [
        ("Quel est le délai ?", "fr", "Je n'ai pas pu formuler de réponse. Veuillez réessayer."),
        ("What is the notice period?", None, "I couldn't produce a response. Please try again."),
        ("merci", "fr", "Je suis là si vous souhaitez continuer."),
        ("ok", None, "I'm here if you'd like to continue."),
    ],
)
def test_empty_response_fallback_answers_in_the_user_language(latest, detected, expected):
    messages = [{"role": "user", "content": latest}]
    assert empty_response_fallback(messages, lambda _text: detected) == expected


@pytest.mark.parametrize(
    ("response_format", "structured"),
    [(None, False), ({"type": "text"}, False), ({"type": "json_object"}, True), ({"type": "json_schema"}, True)],
)
def test_is_structured_output(response_format, structured):
    assert is_structured_output({"response_format": response_format}) is structured


def test_build_extra_payload_keeps_cited_sources_and_reports_them():
    sources = [{"n": 1}, {"n": 2}]
    assert build_extra_payload(sources, {2}, None, include_all_retrieved=False) == {
        "sources": [{"n": 2}],
        "presented_sources": sources,
        "cited_sources": [{"n": 2}],
        "citations_reported": True,
    }


def test_build_extra_payload_keeps_everything_when_nothing_was_reported():
    sources = [{"n": 1}]
    extra = build_extra_payload(sources, None, [{"n": 1}, {"n": 9}], include_all_retrieved=True)
    assert extra["sources"] == sources
    assert extra["cited_sources"] == []
    assert extra["citations_reported"] is False
    assert extra["all_retrieved_sources"] == [{"n": 1}, {"n": 9}]
