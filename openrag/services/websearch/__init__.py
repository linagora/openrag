from .base import BaseWebSearchProvider as BaseWebSearchProvider
from .base import WebResult as WebResult
from .content_fetcher import ContentFetcher as ContentFetcher
from .providers.staan import StaanProvider
from .service import WebSearchService as WebSearchService

PROVIDER_MAPPING = {
    "staan": StaanProvider,
}


class WebSearchFactory:
    @staticmethod
    def create_service(config) -> WebSearchService:
        """Create a WebSearchService from config, following the embedder/retriever pattern."""
        ws_config = config.websearch
        api_token = ws_config.api_token
        max_tokens = ws_config.max_tokens

        if not api_token:
            return WebSearchService(provider=None, max_tokens=max_tokens)

        provider_name = ws_config.provider
        provider_cls = PROVIDER_MAPPING.get(provider_name)
        if provider_cls is None:
            raise ValueError(f"Unsupported web search provider: {provider_name}")

        provider = provider_cls(
            api_token=api_token,
            base_url=ws_config.base_url,
            top_k=ws_config.top_k,
            lang=ws_config.lang,
        )

        content_fetcher = None
        if ws_config.fetch_content:
            content_fetcher = ContentFetcher(
                max_results=ws_config.fetch_max_results,
                timeout=ws_config.fetch_timeout,
                max_tokens_per_page=ws_config.fetch_max_tokens,
                verify_ssl=ws_config.fetch_verify_ssl,
            )

        return WebSearchService(
            provider=provider,
            content_fetcher=content_fetcher,
            max_tokens=max_tokens,
        )
