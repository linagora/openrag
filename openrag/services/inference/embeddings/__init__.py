from .base import BaseEmbedding
from .openai import OpenAIEmbedding

EMBEDDER_MAPPING = {
    "openai": OpenAIEmbedding,
}


class EmbeddingFactory:
    @staticmethod
    def get_embedder(embeddings_config) -> BaseEmbedding:
        provider = embeddings_config.provider
        embedder_class = EMBEDDER_MAPPING.get(provider, None)

        if not embedder_class:
            raise ValueError(f"Unsupported embedding provider: {provider}")

        return embedder_class(embeddings_config)


__all__ = ["BaseEmbedding", "EmbeddingFactory", "OpenAIEmbedding"]
