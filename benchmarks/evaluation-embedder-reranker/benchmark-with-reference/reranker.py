import gc
import copy
import torch
from enum import Enum
from ragatouille import RAGPretrainedModel
from sentence_transformers import CrossEncoder
from loguru import logger
from typing import Literal


class RerankerType(Enum):
    CROSSENCODER = "crossencoder"
    COLBERT = "colbert"


class Reranker:
    def __init__(self, reranker_type=Literal["crossencoder", "colbert"], model_name=""):
        self.reranker_type = RerankerType(reranker_type)
        logger.debug(f"Reranker type=`{self.reranker_type}`, Model name={model_name}")

        match self.reranker_type:
            case RerankerType.CROSSENCODER:
                self.model = CrossEncoder(
                    model_name=model_name,
                    device="cuda" if torch.cuda.is_available() else "cpu",
                    trust_remote_code=True,
                )
            case RerankerType.COLBERT:
                # Initialize ColBERT model here
                self.model = RAGPretrainedModel.from_pretrained(
                    pretrained_model_name_or_path=model_name
                )
            case _:
                raise ValueError(
                    "reranker_type must be either 'crossencoder' or 'colbert'"
                )

        logger.debug(f"{self.reranker_type} Reranker initialized...")

    def rerank(self, query: str, chunks: list[str], top_k: int | None = None):
        top_k = len(chunks) if top_k is None else top_k
        match self.reranker_type:
            case RerankerType.CROSSENCODER:
                reranked_chunks = self.___crossencoder_rerank(query, chunks, top_k)
                return reranked_chunks

            case RerankerType.COLBERT:
                reranked_chunks = self.___colbert_rerank(query, chunks, top_k)
                return reranked_chunks

    def ___crossencoder_rerank(self, query: str, chunks: list, top_k: int):
        with torch.no_grad():
            chunks_txt = [chunk["content"] for chunk in chunks]
            results = self.model.rank(query=query, documents=chunks_txt, top_k=top_k)

        gc.collect()
        torch.cuda.empty_cache()
        return [chunks[r["corpus_id"]] for r in results]

    def ___colbert_rerank(self, query: str, chunks: list, top_k: int):
        with torch.no_grad():
            chunks_txt = [chunk["content"] for chunk in chunks]
            results = self.model.rerank(
                query=query, documents=chunks_txt, k=top_k, bsize="auto"
            )

        gc.collect()
        torch.cuda.empty_cache()
        return [chunk for chunk in original_chunks(results, chunks)]


def original_chunks(ranked_txt: list[str], chunks: list):
    chunks = copy.deepcopy(chunks)
    for chunk_txt in ranked_txt:
        for chunk in chunks:
            if chunk_txt["content"] == chunk["content"]:
                yield chunk
                chunks.remove(chunk)
                break