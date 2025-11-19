import openai
from langchain_core.documents.base import Document
from openai import OpenAI
from utils.exceptions.embeddings import *
from utils.logger import get_logger

from .base import BaseEmbedding

logger = get_logger()


class OpenAIEmbedding(BaseEmbedding):
    def __init__(self, embeddings_config: dict):
        self.embedding_model = embeddings_config.get("model_name")
        self.base_url = embeddings_config.get("base_url")
        self.api_key = embeddings_config.get("api_key")
        self._sync_client = OpenAI(base_url=self.base_url, api_key=self.api_key)

    @property
    def embedding_dimension(self) -> int:
        try:
            # Test call to get embedding dimension
            output = self.embed_documents([Document(page_content="test")])
            return len(output[0])
        except Exception:
            raise

    def embed_documents(self, texts: list[str | Document]) -> list[list[float]]:
        """
        Embed documents using the configured embedder.
        """
        if isinstance(texts[0], Document):
            texts = [doc.page_content for doc in texts]

        try:
            response = self._sync_client.embeddings.create(
                model=self.embedding_model, input=texts
            )
            return [vector.embedding for vector in response.data]

        except openai.APIError as e:
            logger.error("API error in embed_documents", error=str(e))
            raise EmbeddingAPIError(
                f"OpenAI API error during document embedding: {str(e)}",
                model_name=self.embedding_model,
                base_url=self.base_url,
                error=str(e),
            )

        except (IndexError, AttributeError) as e:
            logger.error("Error while accessing embedding data", error=str(e))
            raise EmbeddingResponseError(
                "Failed to retrieve document embeddings due to unexpected response format.",
                model_name=self.embedding_model,
                base_url=self.base_url,
                error=str(e),
            )

        except Exception as e:
            logger.exception("Unexpected error while embedding documents", error=str(e))
            raise UnexpectedEmbeddingError(
                f"Failed to embed documents: {str(e)}",
                model_name=self.embedding_model,
                base_url=self.base_url,
                error=str(e),
            )

    def embed_query(self, text: str) -> list[float]:
        """
        Embed a query using the configured embedder.
        """
        try:
            output = self.embed_documents([Document(page_content=text)])
            return output[0]
        except Exception:
            raise
