"""Backward-compatibility shim — delegates to services.inference.vllm_client.

All new code should import directly from ``services.inference.vllm_client``.
"""

import copy
import json
import warnings

import httpx
from config.models import LLMConfig
from core.utils.logging import get_logger
from services.inference.vllm_client import VLLMClient  # noqa: F401

logger = get_logger()


class _LLMShim:
    """Legacy shim — delegates to ``VLLMClient`` for retry, circuit breaker,
    and connection pooling while preserving the generator-based interface."""

    def __init__(self, llm_config: LLMConfig, logger=None):
        warnings.warn(
            "components.llm.LLM is deprecated — use services.inference.vllm_client.VLLMClient",
            DeprecationWarning,
            stacklevel=2,
        )
        self.logger = logger
        config_kwargs = {k: v for k, v in llm_config.model_dump().items() if k not in ("api_key", "base_url", "model")}
        self._delegate = VLLMClient(
            endpoint=llm_config.base_url,
            model_name=llm_config.model,
            api_key=llm_config.api_key,
            **config_kwargs,
        )

    async def completions(self, request: dict):
        prompt = request.pop("prompt")
        response = await self._delegate.generate(prompt, **request)
        yield response

    async def chat_completion(self, request: dict):
        messages = request.pop("messages")
        stream = request.pop("stream", False)

        if stream:
            async for line in self._delegate.stream_chat(messages, **request):
                yield line
        else:
            resp_dict = await self._delegate.chat(messages, **request)
            yield resp_dict


class LLM:
    """Legacy LLM wrapper. New code should use VLLMClient (via DI) instead."""

    def __init__(self, llm_config, logger=None):
        self.logger = logger
        default_llm_config = llm_config.model_dump()
        self._api_key = default_llm_config.pop("api_key", None)
        self._base_url = default_llm_config.pop("base_url", None)
        self.default_llm_config = default_llm_config

        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }

    def _extract_llm_overrides(self, request: dict):
        metadata = request.get("metadata") or {}
        llm_override = metadata.pop("llm_override", None) or {}

        request.pop("model")
        payload = copy.deepcopy(self.default_llm_config)
        payload.update(request)

        if llm_override.get("model"):
            payload["model"] = llm_override["model"]

        base_url = (llm_override.get("base_url") or self._base_url).rstrip("/")
        api_key = llm_override.get("api_key") or self._api_key
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }

        return payload, base_url, headers

    async def completions(self, request: dict):
        payload, base_url, headers = self._extract_llm_overrides(request)

        timeout = httpx.Timeout(4 * 10)
        async with httpx.AsyncClient(timeout=timeout) as client:
            try:
                response = await client.post(
                    url=f"{base_url}/completions",
                    headers=headers,
                    json=payload,
                )
                response.raise_for_status()
                data = response.json()
                yield data
            except httpx.HTTPStatusError as e:
                error_detail = e.response.text
                raise ValueError(f"LLM API error ({e.response.status_code}): {error_detail}")
            except json.JSONDecodeError as e:
                raise ValueError(f"Invalid JSON in API response: {str(e)}")

    async def chat_completion(self, request: dict):
        payload, base_url, headers = self._extract_llm_overrides(request)
        stream = payload["stream"]

        timeout = httpx.Timeout(4 * 60)
        async with httpx.AsyncClient(timeout=timeout) as client:
            if stream:
                try:
                    async with client.stream(
                        "POST",
                        url=f"{base_url}/chat/completions",
                        headers=headers,
                        json=payload,
                    ) as response:
                        if response.status_code >= 400:
                            await response.aread()
                            error_detail = response.text
                            raise ValueError(f"LLM API error ({response.status_code}): {error_detail}")
                        async for line in response.aiter_lines():
                            yield line
                except ValueError:
                    raise
                except Exception as e:
                    logger.error(f"Error while streaming chat completion: {str(e)}")
                    raise

            else:
                try:
                    response = await client.post(
                        url=f"{base_url}/chat/completions",
                        headers=headers,
                        json=payload,
                    )
                    response.raise_for_status()
                    data = response.json()
                    yield data
                except httpx.HTTPStatusError as e:
                    error_detail = e.response.text
                    raise ValueError(f"LLM API error ({e.response.status_code}): {error_detail}")
                except json.JSONDecodeError as e:
                    raise ValueError(f"Invalid JSON in API response: {str(e)}")
