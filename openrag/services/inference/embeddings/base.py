from langchain_core.documents.base import Document
from langchain_core.embeddings import Embeddings


class BaseEmbedding(Embeddings):
    """Base class for all embedding models."""

    @property
    def embedding_dimension(self) -> int:
        """
        Returns the dimension of the embedding vector.
        This is used to validate the vector size during indexing and searching.
        """
        raise NotImplementedError("This method should be implemented by subclasses.")

    def embed_documents(self, texts: list[str | Document]) -> list[list[float]]:
        return super().embed_documents(texts)

    def embed_query(self, text: str) -> list[float]:
        return super().embed_query(text)
