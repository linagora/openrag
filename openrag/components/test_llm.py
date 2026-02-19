import pytest
from components.llm import LLM


@pytest.fixture
def llm():
    return LLM(
        {
            "base_url": "http://default-llm:8000/v1",
            "api_key": "default-key",
            "model": "default-model",
            "temperature": 0.3,
        }
    )


class TestExtractLlmOverrides:
    def test_no_override_uses_defaults(self, llm):
        request = {
            "model": "openrag-my-partition",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": False,
        }
        payload, base_url, headers = llm._extract_llm_overrides(request)

        assert payload["model"] == "default-model"
        assert payload["temperature"] == 0.3
        assert base_url == "http://default-llm:8000/v1"
        assert headers["Authorization"] == "Bearer default-key"

    def test_override_all_fields(self, llm):
        request = {
            "model": "openrag-my-partition",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": False,
            "metadata": {
                "llm_override": {
                    "base_url": "http://custom-llm:9000/v1",
                    "api_key": "custom-key",
                    "model": "custom-model",
                }
            },
        }
        payload, base_url, headers = llm._extract_llm_overrides(request)

        assert payload["model"] == "custom-model"
        assert base_url == "http://custom-llm:9000/v1"
        assert headers["Authorization"] == "Bearer custom-key"

    def test_trailing_slash_stripped_from_base_url(self, llm):
        request = {
            "model": "openrag-my-partition",
            "stream": False,
            "metadata": {"llm_override": {"base_url": "http://custom:8000/v1///"}},
        }
        _, base_url, _ = llm._extract_llm_overrides(request)

        assert base_url == "http://custom:8000/v1"

    def test_request_params_forwarded_to_payload(self, llm):
        request = {
            "model": "openrag-my-partition",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": True,
            "max_tokens": 2048,
            "temperature": 0.9,
        }
        payload, _, _ = llm._extract_llm_overrides(request)

        assert payload["stream"] is True
        assert payload["max_tokens"] == 2048
        assert payload["temperature"] == 0.9
        assert payload["messages"] == [{"role": "user", "content": "hello"}]

    def test_metadata_without_llm_override_uses_defaults(self, llm):
        request = {
            "model": "openrag-my-partition",
            "stream": False,
            "metadata": {"use_map_reduce": True},
        }
        payload, base_url, headers = llm._extract_llm_overrides(request)

        assert payload["model"] == "default-model"
        assert base_url == "http://default-llm:8000/v1"
        assert headers["Authorization"] == "Bearer default-key"

    def test_llm_override_popped_from_metadata(self, llm):
        metadata = {
            "use_map_reduce": False,
            "llm_override": {"model": "custom"},
        }
        request = {"model": "x", "stream": False, "metadata": metadata}
        llm._extract_llm_overrides(request)

        assert "llm_override" not in metadata
        assert "use_map_reduce" in metadata
