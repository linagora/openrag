from components.websearch.base import BaseWebSearchProvider, WebResult
from utils.logger import get_logger

logger = get_logger()


class WebSearchService:
    def __init__(self, provider: BaseWebSearchProvider | None):
        self.provider = provider  # None when WEBSEARCH_API_TOKEN is not set

    async def search(self, query: str) -> list[WebResult]:
        if self.provider is None:
            logger.warning("Web search requested but no provider configured — ignoring websearch flag")
            return []
        try:
            results = await self.provider.search(query)
            if not results:
                logger.warning("Web search returned zero results", query=query)
            return results
        except Exception as e:
            logger.warning("Web search failed, continuing without web context", error=str(e))
            return []
