import httpx
from components.websearch.base import BaseWebSearchProvider, WebResult
from utils.logger import get_logger

logger = get_logger()


class StaanProvider(BaseWebSearchProvider):
    def __init__(self, api_token: str, base_url: str, top_k: int = 5, lang: str = "fr-FR"):
        self.api_token = api_token
        self.base_url = base_url
        self.top_k = top_k
        self.lang = lang

    async def search(self, query: str) -> list[WebResult]:
        headers = {"Authorization": f"Bearer {self.api_token}"}
        params = {"q": query, "market": self.lang, "offset": 0}
        timeout = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=2.0)

        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(self.base_url, headers=headers, params=params)
            response.raise_for_status()
            data = response.json()

        results = data if isinstance(data, list) else data.get("web", {}).get("results", [])
        return [
            WebResult(
                title=r.get("title", ""),
                url=r.get("url", ""),
                snippet=r.get("snippet", ""),
                display_url=r.get("display_url"),
                hostname=r.get("hostname"),
            )
            for r in results[: self.top_k]
        ]
