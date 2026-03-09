from components.websearch.base import BaseWebSearchProvider, WebResult
from components.websearch.content_fetcher import ContentFetcher
from utils.logger import get_logger

logger = get_logger()


class WebSearchService:
    def __init__(
        self,
        provider: BaseWebSearchProvider | None,
        content_fetcher: ContentFetcher | None = None,
        max_tokens: int = 2000,
    ):
        self.provider = provider  # None when WEBSEARCH_API_TOKEN is not set
        self.content_fetcher = content_fetcher
        self.max_tokens = max_tokens

    async def search(self, query: str) -> list[WebResult]:
        if self.provider is None:
            logger.warning("Web search requested but no provider configured — ignoring websearch flag")
            return []
        try:
            results = await self.provider.search(query)
            if not results:
                logger.warning("Web search returned zero results", query=query)
                return results

            if self.content_fetcher:
                results = await self.content_fetcher.enrich(results)

            return results
        except Exception as e:
            logger.warning("Web search failed, continuing without web context", error=str(e))
            return []
