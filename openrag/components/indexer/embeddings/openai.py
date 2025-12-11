import openai
from langchain_core.documents.base import Document
from openai import OpenAI
from utils.exceptions.embeddings import *
from utils.logger import get_logger

from .base import BaseEmbedding

logger = get_logger()


class OpenAIEmbedding(BaseEmbedding):
    def __init__(self, embeddings_config: dict):
        """
        Initialize the embedding backend from a configuration dictionary and create a synchronous OpenAI client.
        
        Parameters:
            embeddings_config (dict): Configuration mapping with keys:
                - model_name (str): Name of the embedding model to use.
                - base_url (str): Base URL for the OpenAI-compatible API.
                - api_key (str): API key for authenticating requests.
                - max_model_len (int, optional): Maximum token length to use for truncation; defaults to 8192.
        
        Side effects:
            Sets instance attributes `embedding_model`, `base_url`, `api_key`, `max_model_len` and initializes a synchronous OpenAI client at `self._sync_client`.
        """
        self.embedding_model = embeddings_config.get("model_name")
        self.base_url = embeddings_config.get("base_url")
        self.api_key = embeddings_config.get("api_key")
        self.max_model_len = embeddings_config.get("max_model_len", 8192)
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
        Compute embedding vectors for a list of input strings or Document objects.
        
        Given a list of strings or a list of Document instances, returns an embedding vector for each input in the same order. If Documents are provided, their page_content is used as the input text.
        
        Parameters:
            texts (list[str | Document]): Input items to embed; each item is either a raw text string or a Document whose page_content will be embedded.
        
        Returns:
            list[list[float]]: A list where each element is an embedding vector (list of floats) corresponding to the input at the same index.
        
        Raises:
            EmbeddingAPIError: If the embedding API returns an error.
            EmbeddingResponseError: If the API response has an unexpected format or missing embedding data.
            UnexpectedEmbeddingError: For any other failures that occur while generating embeddings.
        """
        if isinstance(texts[0], Document):
            texts = [doc.page_content for doc in texts]

        try:
            response = self._sync_client.embeddings.create(
                model=self.embedding_model,
                input=texts,
                extra_body={"truncate_prompt_tokens": self.max_model_len},
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