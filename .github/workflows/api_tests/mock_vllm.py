"""
Lightweight mock VLLM embedding server for CI testing.
Returns deterministic fake embeddings without loading actual models.
"""
import hashlib
from typing import List, Union

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()

# Matches ibm-granite/granite-embedding-small-english-r2 dimension
EMBEDDING_DIM = 384


class EmbeddingRequest(BaseModel):
    model: str
    input: Union[str, List[str]]
    encoding_format: str = "float"


class EmbeddingData(BaseModel):
    object: str = "embedding"
    embedding: List[float]
    index: int


class EmbeddingResponse(BaseModel):
    object: str = "list"
    data: List[EmbeddingData]
    model: str
    usage: dict


def generate_fake_embedding(text: str, dim: int = EMBEDDING_DIM) -> List[float]:
    """Generate deterministic fake embedding based on text hash."""
    h = hashlib.md5(text.encode()).digest()
    result = []
    for i in range(dim):
        byte_val = h[i % len(h)]
        result.append((byte_val / 128.0) - 1.0)
    return result


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [{"id": "mock-embedding-model", "object": "model"}]
    }


@app.post("/v1/embeddings")
async def create_embeddings(request: EmbeddingRequest) -> EmbeddingResponse:
    inputs = request.input if isinstance(request.input, list) else [request.input]

    data = [
        EmbeddingData(
            embedding=generate_fake_embedding(text),
            index=i
        )
        for i, text in enumerate(inputs)
    ]

    return EmbeddingResponse(
        data=data,
        model=request.model,
        usage={"prompt_tokens": len(inputs) * 10, "total_tokens": len(inputs) * 10}
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
