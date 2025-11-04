import asyncio
from datetime import datetime, timezone
from infinity_client import Client
from infinity_client.api.default import rerank
from infinity_client.models import RerankInput, ReRankResult
from langchain_core.documents.base import Document


class Reranker:
    def __init__(self, logger, config):
        self.model_name = config.reranker["model_name"]
        self.client = Client(base_url=config.reranker["base_url"])
        self.logger = logger
        self.semaphore = asyncio.Semaphore(
            5
        )  # Only allow 5 reranking operation at a time
        
        # Temporal scoring parameters
        self.temporal_weight = config.reranker.get("temporal_weight", 0.3)  # Weight for temporal scoring
        self.temporal_decay_days = config.reranker.get("temporal_decay_days", 365)  # Days for full decay
        
        self.logger.debug(
            "Reranker initialized",
            model_name=self.model_name,
            temporal_weight=self.temporal_weight,
            temporal_decay_days=self.temporal_decay_days,
        )

    def _calculate_temporal_score(self, doc: Document) -> float:
        """
        Calculate temporal score based on document recency.
        More recent documents get higher scores (0.0 to 1.0).
        
        Uses linear decay based on document age.
        Priority: datetime > modified_at > created_at > indexed_at
        """
        try:
            # Try datetime first (user-provided), then modified_at, then created_at, then indexed_at
            date_str = (
                doc.metadata.get("datetime") or 
                doc.metadata.get("modified_at") or 
                doc.metadata.get("created_at") or 
                doc.metadata.get("indexed_at")
            )
            
            if not date_str:
                # No temporal information, return neutral score
                return 0.5
            
            # Parse the date and ensure it's UTC-aware
            if 'T' in date_str:
                doc_date = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
            else:
                doc_date = datetime.fromisoformat(date_str)
            
            # Ensure timezone awareness - assume UTC if naive
            if doc_date.tzinfo is None:
                doc_date = doc_date.replace(tzinfo=timezone.utc)
            
            # Calculate age in days using UTC now
            now = datetime.now(timezone.utc)
            days_old = (now - doc_date).total_seconds() / 86400
            
            # Linear decay formula
            # Score decreases linearly from 1.0 (today) to 0.0 (temporal_decay_days ago)
            temporal_score = max(0.0, min(1.0, (1.0 - days_old / self.temporal_decay_days)))
            
            return temporal_score
            
        except Exception as e:
            self.logger.warning(f"Error calculating temporal score: {e}")
            return 0.5  # Neutral score on error

    async def rerank(
        self, query: str, documents: list[Document], top_k: int
    ) -> list[Document]:
        async with self.semaphore:
            self.logger.debug(
                "Reranking documents", documents_count=len(documents), top_k=top_k
            )
            top_k = min(top_k, len(documents))
            rerank_input = RerankInput.from_dict(
                {
                    "model": self.model_name,
                    "query": query,
                    "documents": [doc.page_content for doc in documents],
                    "top_n": top_k,
                    "return_documents": True,
                    "raw_scores": True,
                }
            )
            try:
                rerank_result: ReRankResult = await rerank.asyncio(
                    client=self.client, body=rerank_input
                )
                output = []
                for rerank_res in rerank_result.results:
                    doc = documents[rerank_res.index]
                    relevance_score = rerank_res.relevance_score
                    
                    # Calculate temporal score
                    temporal_score = self._calculate_temporal_score(doc)
                    
                    # Combine relevance and temporal scores
                    # Final score = (1 - temporal_weight) * relevance + temporal_weight * temporal
                    combined_score = (
                        (1 - self.temporal_weight) * relevance_score +
                        self.temporal_weight * temporal_score
                    )
                    
                    # Store all scores in metadata
                    doc.metadata["relevance_score"] = relevance_score
                    doc.metadata["temporal_score"] = temporal_score
                    doc.metadata["combined_score"] = combined_score
                    
                    output.append(doc)
                
                # Re-sort by combined score (descending)
                output.sort(key=lambda d: d.metadata.get("combined_score", 0), reverse=True)
                
                self.logger.debug(
                    "Reranking complete with temporal scoring",
                    documents_returned=len(output),
                    temporal_weight=self.temporal_weight,
                )
                
                return output

            except Exception as e:
                self.logger.error(
                    "Reranking failed",
                    error=str(e),
                    model_name=self.model_name,
                    documents_count=len(documents),
                )
                raise e
