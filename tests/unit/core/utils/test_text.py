from __future__ import annotations

import sys
from types import SimpleNamespace


def test_get_num_tokens_does_not_pass_enable_thinking_to_chatopenai(monkeypatch):
    import core.utils.text as text_utils

    captured: dict = {}

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def get_num_tokens(self, _text: str) -> int:
            return 1

    monkeypatch.setitem(sys.modules, "langchain_openai", SimpleNamespace(ChatOpenAI=FakeChatOpenAI))
    monkeypatch.setattr(
        text_utils,
        "load_config",
        lambda: SimpleNamespace(
            llm=SimpleNamespace(
                model_dump=lambda: {
                    "base_url": "http://llm:8000/v1",
                    "model": "qwen",
                    "api_key": "key",
                    "enable_thinking": False,
                }
            )
        ),
    )
    monkeypatch.setattr(text_utils, "_cached_length_function", None)

    length_function = text_utils.get_num_tokens()

    assert length_function("hello") == 1
    assert "enable_thinking" not in captured
