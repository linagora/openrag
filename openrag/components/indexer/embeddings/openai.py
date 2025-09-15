import openai
from langchain_core.documents.base import Document
from openai import AsyncOpenAI, OpenAI
from utils.exceptions.embeddings import *
from utils.logger import get_logger

from .base import BaseEmbedding

logger = get_logger()


class OpenAIEmbedding(BaseEmbedding):
    def __init__(self, embeddings_config: dict):
        self.embedding_model = embeddings_config.get("model_name")
        self.base_url = embeddings_config.get("base_url")
        self.api_key = embeddings_config.get("api_key")

        self._async_client = AsyncOpenAI(base_url=self.base_url, api_key=self.api_key)
        self._sync_client = OpenAI(base_url=self.base_url, api_key=self.api_key)

    @property
    def embedding_dimension(self) -> int:
        try:
            embedding_vect = (
                self._sync_client.embeddings.create(
                    model=self.embedding_model,
                    input=["test"],
                )
                .data[0]
                .embedding
            )
            return len(embedding_vect)

        except openai.APIError as e:
            logger.error(f"API error while getting embedding dimension: {str(e)}")
            raise EmbeddingAPIError(
                f"Failed to get embedding dimension: {str(e)}",
                model_name=self.embedding_model,
                base_url=self.base_url,
                provider_error=str(e),
            )

        except (IndexError, AttributeError) as e:
            logger.error("Error while accessing embedding data", error=str(e))
            raise EmbeddingResponseError(
                "Failed to retrieve embedding dimension due to unexpected response format.",
                model_name=self.embedding_model,
                provider_error=str(e),
            )

        except Exception as e:
            logger.exception(
                "Unexpected error while getting embedding dimension", error=str(e)
            )
            raise UnexpectedEmbeddingError(
                f"Failed to get embedding dimension: {str(e)}",
                model_name=self.embedding_model,
                provider_error=str(e),
            )

    async def embed_documents(self, chunks: list[Document]) -> list[dict]:
        """
        Asynchronously embed documents using the configured embedder.
        """
        try:
            output = []
            texts = [chunk.page_content for chunk in chunks]
            embeddings = await self._async_client.embeddings.create(
                model=self.embedding_model,
                input=texts,
            )

            for i, chunk in enumerate(chunks):
                output.append(
                    {
                        "text": chunk.page_content,
                        "vector": embeddings.data[i].embedding,
                        **chunk.metadata,
                    }
                )
            return output
        except openai.APIError as e:
            logger.error("API error in embed_documents", error=str(e))
            raise EmbeddingAPIError(
                f"OpenAI API error during document embedding: {str(e)}",
                model_name=self.embedding_model,
                base_url=self.base_url,
                provider_error=str(e),
            )

        except (IndexError, AttributeError) as e:
            logger.error("Error while accessing embedding data", error=str(e))
            raise EmbeddingResponseError(
                "Failed to retrieve document embeddings due to unexpected response format.",
                model_name=self.embedding_model,
                provider_error=str(e),
            )

        except Exception as e:
            logger.exception("Unexpected error while embedding documents", error=str(e))
            raise UnexpectedEmbeddingError(
                f"Failed to embed documents: {str(e)}",
                model_name=self.embedding_model,
                provider_error=str(e),
            )

    async def embed_query(self, query: str) -> list[float]:
        """
        Asynchronously embed a query using the configured embedder.
        """

        try:
            embedding = await self._async_client.embeddings.create(
                model=self.embedding_model,
                input=[query],
            )
            res = embedding.data[0].embedding
            return res

        except openai.APIError as e:
            logger.error("API error in embed_query", error=str(e))
            raise EmbeddingAPIError(
                f"OpenAI API error during query embedding: {str(e)}",
                model_name=self.embedding_model,
                base_url=self.base_url,
                provider_error=str(e),
            )

        except (IndexError, AttributeError) as e:
            logger.error("Error while accessing embedding data", error=str(e))
            raise EmbeddingResponseError(
                "Failed to retrieve query embedding due to unexpected response format.",
                model_name=self.embedding_model,
                provider_error=str(e),
            )

        except Exception as e:
            logger.exception("Unexpected error while embedding query", error=str(e))
            raise UnexpectedEmbeddingError(
                f"Failed to embed query: {str(e)}",
                model_name=self.embedding_model,
                provider_error=str(e),
            )
