import json

import httpx
import copy
from utils.logger import get_logger

logger = get_logger()


class LLM:
    def __init__(self, llm_config, logger=None):
        self.logger = logger
        default_llm_config = dict(llm_config)
        self._api_key = default_llm_config.pop("api_key", None)
        self._base_url = default_llm_config.pop("base_url", None)
        self.default_llm_config = default_llm_config

        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }

    async def completions(self, request: dict):
        request.pop("model")
        payload = copy.deepcopy(self.default_llm_config)
        payload.update(request)

        timeout = httpx.Timeout(4 * 10)
        async with httpx.AsyncClient(timeout=timeout) as client:
            try:
                response = await client.post(
                    url=f"{self._base_url}completions",
                    headers=self.headers,
                    json=payload,
                )
                response.raise_for_status()
                data = response.json()
                yield data
            except httpx.HTTPStatusError as e:
                error_detail = e.response.text
                raise ValueError(
                    f"LLM API error ({e.response.status_code}): {error_detail}"
                )
            except json.JSONDecodeError as e:
                raise ValueError(f"Invalid JSON in API response: {str(e)}")

    async def chat_completion(self, request: dict):
        request.pop("model")
        payload = copy.deepcopy(self.default_llm_config)
        payload.update(request)
        stream = request["stream"]

        timeout = httpx.Timeout(4 * 60)
        async with httpx.AsyncClient(timeout=timeout) as client:
            if stream:
                try:
                    async with client.stream(
                        "POST",
                        url=f"{self._base_url}chat/completions",
                        headers=self.headers,
                        json=payload,
                    ) as response:
                        if response.status_code >= 400:
                            await response.aread()
                            error_detail = response.text
                            raise ValueError(
                                f"LLM API error ({response.status_code}): {error_detail}"
                            )
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
                        url=f"{self._base_url}chat/completions",
                        headers=self.headers,
                        json=payload,
                    )
                    response.raise_for_status()
                    data = response.json()
                    yield data
                except httpx.HTTPStatusError as e:
                    error_detail = e.response.text
                    raise ValueError(
                        f"LLM API error ({e.response.status_code}): {error_detail}"
                    )
                except json.JSONDecodeError as e:
                    raise ValueError(f"Invalid JSON in API response: {str(e)}")
