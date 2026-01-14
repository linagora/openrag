"""
Lightweight mock VLLM server for CI testing.
Provides fake embeddings and chat completions without loading actual models.
"""

import hashlib
import time
import uuid
from typing import Any, List, Optional, Union

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()

# Matches ibm-granite/granite-embedding-small-english-r2 dimension
EMBEDDING_DIM = 384


# ============== Embedding Models ==============


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


# ============== Chat Completion Models ==============


class ChatMessage(BaseModel):
    role: str
    content: Any  # Can be string or list (for vision models)


class ChatCompletionRequest(BaseModel):
    model: str
    messages: List[ChatMessage]
    temperature: Optional[float] = 0.7
    max_tokens: Optional[int] = 1024
    stream: Optional[bool] = False
    top_p: Optional[float] = 1.0
    n: Optional[int] = 1
    stop: Optional[Union[str, List[str]]] = None


class ChatCompletionChoice(BaseModel):
    index: int
    message: ChatMessage
    finish_reason: str = "stop"


class ChatCompletionUsage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[ChatCompletionChoice]
    usage: ChatCompletionUsage


# ============== Text Completion Models ==============


class TextCompletionRequest(BaseModel):
    model: str
    prompt: Union[str, List[str]]
    temperature: Optional[float] = 0.7
    max_tokens: Optional[int] = 1024
    stream: Optional[bool] = False
    top_p: Optional[float] = 1.0
    n: Optional[int] = 1
    stop: Optional[Union[str, List[str]]] = None


class TextCompletionChoice(BaseModel):
    index: int
    text: str
    finish_reason: str = "stop"


class TextCompletionResponse(BaseModel):
    id: str
    object: str = "text_completion"
    created: int
    model: str
    choices: List[TextCompletionChoice]
    usage: ChatCompletionUsage  # Same structure as chat


# ============== Helper Functions ==============


def generate_fake_embedding(text: str, dim: int = EMBEDDING_DIM) -> List[float]:
    """Generate deterministic fake embedding based on text hash."""
    h = hashlib.md5(text.encode()).digest()
    result = []
    for i in range(dim):
        byte_val = h[i % len(h)]
        result.append((byte_val / 128.0) - 1.0)
    return result


def count_tokens(text: str) -> int:
    """Approximate token count (roughly 4 chars per token)."""
    if isinstance(text, str):
        return max(1, len(text) // 4)
    return 10  # Default for non-string content


def generate_mock_response(messages: List[ChatMessage]) -> str:
    """Generate a mock response based on the input messages."""
    last_message = messages[-1] if messages else None
    if not last_message:
        return "Mock response"

    content = last_message.content
    if isinstance(content, list):
        # Vision model request - extract text parts
        text_parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
        content = " ".join(text_parts) if text_parts else "image analysis request"

    # Generate contextual mock responses
    content_lower = str(content).lower()

    if "contextualize" in content_lower or "context" in content_lower:
        return "This chunk discusses the main topic of the document and provides relevant context for understanding the content."

    if "describe" in content_lower or "image" in content_lower:
        return "This is an image showing relevant content from the document."

    if "summarize" in content_lower:
        return "This is a summary of the provided content."

    # Default response
    return f"Mock response to: {str(content)[:100]}"


# ============== Endpoints ==============


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [
            {"id": "mock-embedding-model", "object": "model"},
            {"id": "mock-chat-model", "object": "model"},
            {"id": "mock-vlm-model", "object": "model"},
        ],
    }


@app.post("/v1/embeddings")
async def create_embeddings(request: EmbeddingRequest) -> EmbeddingResponse:
    inputs = request.input if isinstance(request.input, list) else [request.input]

    data = [
        EmbeddingData(embedding=generate_fake_embedding(text), index=i)
        for i, text in enumerate(inputs)
    ]

    return EmbeddingResponse(
        data=data,
        model=request.model,
        usage={"prompt_tokens": len(inputs) * 10, "total_tokens": len(inputs) * 10},
    )


@app.post("/v1/chat/completions")
async def create_chat_completion(request: ChatCompletionRequest) -> ChatCompletionResponse:
    """Mock chat completion endpoint for LLM/VLM requests."""
    # Calculate token counts
    prompt_tokens = sum(
        count_tokens(str(msg.content)) for msg in request.messages
    )

    # Generate mock response
    response_text = generate_mock_response(request.messages)
    completion_tokens = count_tokens(response_text)

    return ChatCompletionResponse(
        id=f"chatcmpl-{uuid.uuid4().hex[:8]}",
        created=int(time.time()),
        model=request.model,
        choices=[
            ChatCompletionChoice(
                index=0,
                message=ChatMessage(role="assistant", content=response_text),
                finish_reason="stop",
            )
        ],
        usage=ChatCompletionUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
    )


@app.post("/v1/completions")
async def create_text_completion(request: TextCompletionRequest) -> TextCompletionResponse:
    """Mock text completion endpoint (non-chat)."""
    prompts = request.prompt if isinstance(request.prompt, list) else [request.prompt]
    prompt_tokens = sum(count_tokens(p) for p in prompts)

    # Generate simple mock response
    response_text = f"Mock completion for: {prompts[0][:50]}..."
    completion_tokens = count_tokens(response_text)

    return TextCompletionResponse(
        id=f"cmpl-{uuid.uuid4().hex[:8]}",
        created=int(time.time()),
        model=request.model,
        choices=[
            TextCompletionChoice(
                index=0,
                text=response_text,
                finish_reason="stop",
            )
        ],
        usage=ChatCompletionUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
