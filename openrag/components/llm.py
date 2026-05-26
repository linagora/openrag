import copy
import json

import httpx
from config import load_config
from utils.logger import get_logger

logger = get_logger()


class LLM:
    def __init__(self, llm_config, logger=None):
        self.logger = logger
        default_llm_config = llm_config.model_dump()
        self._api_key = default_llm_config.pop("api_key", None)
        self._base_url = default_llm_config.pop("base_url", None)
        # Force the max_tokens in default config
        default_llm_config["max_tokens"] = load_config().llm_context.max_output_tokens
        self.default_llm_config = default_llm_config

        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }

    def _extract_llm_overrides(self, request: dict):
        """Build the upstream payload, optionally applying llm_override.

        - Default path (no llm_override): merge `default_llm_config` with request params
        - Override path: forward only what the client explicitly sent, to avoid unsupported params
        """
        metadata = request.get("metadata") or {}
        llm_override = metadata.pop("llm_override", None) or {}

        # `model` and `metadata` are openrag-specific. No need to transmit to LLM
        request.pop("model", None)
        request.pop("metadata", None)

        if llm_override:
            payload = copy.deepcopy(request)
            payload["model"] = llm_override.get("model") or self.default_llm_config.get("model", "")
        else:
            payload = copy.deepcopy(self.default_llm_config)
            payload.update(request)

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
        stream = payload.get("stream", False)

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

            else:  # Handle non-streaming response
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
