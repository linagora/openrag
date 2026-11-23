"""AnswerService: the answering step shared by chat, streaming chat and text completions."""

from __future__ import annotations

import json

import pytest
from services.orchestrators.answer_service import AnswerService, PreparedAnswer


class FakeLLM:
    def __init__(self, *, chat_contents=(), gen_text="", stream_lines=()):
        self._chat_contents = list(chat_contents)
        self._gen_text = gen_text
        self._stream_lines = list(stream_lines)
        self.chat_calls: list = []
        self.generate_calls: list = []

    async def chat(self, messages, **kwargs):
        self.chat_calls.append((messages, kwargs))
        return {"choices": [{"message": {"content": self._chat_contents.pop(0)}, "finish_reason": "stop"}]}

    async def generate(self, prompt, **kwargs):
        self.generate_calls.append((prompt, kwargs))
        return {"choices": [{"text": self._gen_text}]}

    async def stream_chat(self, messages, **kwargs):
        for line in self._stream_lines:
            yield line


def _service(language: str | None = "en") -> AnswerService:
    return AnswerService(detect_language=lambda _text: language)


def _prepared(**overrides) -> PreparedAnswer:
    fields = {
        "payload": {
            "messages": [{"role": "system", "content": "sys"}, {"role": "user", "content": "q"}],
            "model": "openrag-p",
            "stream": False,
            "temperature": 0.1,
        },
        "sources": [{"n": 1}, {"n": 2}],
        "citation_protocol_active": True,
    }
    return PreparedAnswer(**{**fields, **overrides})


@pytest.mark.asyncio
async def test_chat_strips_the_citation_tag_and_keeps_the_cited_sources():
    llm = FakeLLM(chat_contents=["Three months. [Sources: 2]"])

    chunk = await _service().chat(_prepared(), llm=llm, model_name="openrag-p")

    assert chunk["model"] == "openrag-p"
    assert chunk["choices"][0]["message"]["content"] == "Three months."
    assert chunk["extra"]["cited_sources"] == [{"n": 2}]
    assert llm.chat_calls[0][1] == {"temperature": 0.1}, "transport keys are not sampling params"


@pytest.mark.asyncio
async def test_chat_puts_the_extra_fields_then_the_trace_after_the_sources():
    prepared = _prepared(
        extra_fields={"attachments": ["f1"]},
        finish_trace=lambda: {"retrieval_trace": {"stages": []}},
    )

    chunk = await _service().chat(prepared, llm=FakeLLM(chat_contents=["ok [Sources: 1]"]), model_name="m")

    assert list(chunk["extra"]) == [
        "sources",
        "presented_sources",
        "cited_sources",
        "citations_reported",
        "attachments",
        "retrieval_trace",
    ]


@pytest.mark.asyncio
async def test_chat_retries_an_empty_answer_once_then_falls_back():
    llm = FakeLLM(chat_contents=["[Sources: 1]", ""])

    chunk = await _service(language="fr").chat(_prepared(), llm=llm, model_name="m")

    assert len(llm.chat_calls) == 2
    assert "previous response was empty" in llm.chat_calls[1][0][0]["content"]
    assert chunk["choices"][0]["message"]["content"] == "Je n'ai pas pu formuler de réponse. Veuillez réessayer."


@pytest.mark.asyncio
async def test_chat_without_the_citation_protocol_keeps_the_text_as_it_is():
    llm = FakeLLM(chat_contents=["Hello [Sources: 1]"])

    chunk = await _service().chat(_prepared(citation_protocol_active=False), llm=llm, model_name="m")

    assert chunk["choices"][0]["message"]["content"] == "Hello [Sources: 1]"
    assert chunk["extra"]["citations_reported"] is False


@pytest.mark.asyncio
async def test_chat_stream_ends_with_the_extra_and_the_trace():
    llm = FakeLLM(
        stream_lines=[
            'data: {"choices":[{"delta":{"content":"Three months. [Sources: 1]"}}]}\n\n',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
            "data: [DONE]\n\n",
        ]
    )
    prepared = _prepared(extra_fields={"attachments": []}, finish_trace=lambda: {"retrieval_trace": {}})

    lines = [line async for line in _service().chat_stream(prepared, llm=llm, model_name="m")]

    events = [json.loads(line[len("data: ") :]) for line in lines if line.strip() != "data: [DONE]"]
    text = "".join(e["choices"][0].get("delta", {}).get("content", "") for e in events if e.get("choices"))
    assert text == "Three months."
    extra = events[-1]["extra"]
    assert extra["cited_sources"] == [{"n": 1}]
    assert extra["attachments"] == []
    assert extra["retrieval_trace"] == {}


@pytest.mark.asyncio
async def test_complete_strips_the_citation_tag():
    llm = FakeLLM(gen_text="Three months. [Sources: 1]")
    prepared = _prepared(payload={"prompt": "q", "model": "openrag-p", "max_tokens": 10})

    resp = await _service().complete(prepared, llm=llm)

    assert resp["choices"][0]["text"] == "Three months."
    assert resp["extra"]["cited_sources"] == [{"n": 1}]
    assert llm.generate_calls == [("q", {"max_tokens": 10})]
