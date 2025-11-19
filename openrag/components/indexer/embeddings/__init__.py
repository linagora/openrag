from .base import BaseEmbedding
from .openai import OpenAIEmbedding

EMBEDDER_MAPPING = {
    "openai": OpenAIEmbedding,
}


class EmbeddingFactory:
    @staticmethod
    def get_embedder(embeddings_config: dict) -> BaseEmbedding:
        provider = embeddings_config.get("provider")
        embedder_class = EMBEDDER_MAPPING.get(provider, None)

        if not embedder_class:
            raise ValueError(f"Unsupported embedding provider: {provider}")

        return embedder_class(embeddings_config)


__all__ = ["EmbeddingFactory", "BaseEmbedding", "OpenAIEmbedding"]
