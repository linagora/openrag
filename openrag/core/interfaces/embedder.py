from abc import ABC, abstractmethod


class Embedder(ABC):
    """Abstract interface for text embedding models.

    Implementations: OpenAIEmbedding (components/indexer/embeddings/openai.py)
    """

    @property
    @abstractmethod
    def embedding_dimension(self) -> int:
        """Return the dimension of the embedding vector."""
        ...

    @abstractmethod
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed a list of text strings into vectors."""
        ...

    @abstractmethod
    def embed_query(self, text: str) -> list[float]:
        """Embed a single query string into a vector."""
        ...
