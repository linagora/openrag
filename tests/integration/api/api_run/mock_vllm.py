"""
Lightweight mock VLLM server for testing.
Provides fake embeddings and chat completions without loading actual models.
"""

import hashlib
import json
import time
import uuid
from typing import Any

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict

app = FastAPI()

# Matches ibm-granite/granite-embedding-small-english-r2 dimension
EMBEDDING_DIM = 384


class EmbeddingRequest(BaseModel):
    model: str
    input: str | list[str]
    encoding_format: str = "float"


class EmbeddingData(BaseModel):
    object: str = "embedding"
    embedding: list[float]
    index: int


class EmbeddingResponse(BaseModel):
    object: str = "list"
    data: list[EmbeddingData]
    model: str
    usage: dict


class ChatMessage(BaseModel):
    role: str
    content: Any


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str
    messages: list[ChatMessage]
    temperature: float | None = 0.7
    max_tokens: int | None = 1024
    stream: bool | None = False
    top_p: float | None = 1.0
    n: int | None = 1
    stop: str | list[str] | None = None
    tools: list[Any] | None = None
    tool_choice: Any | None = None


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
    choices: list[ChatCompletionChoice]
    usage: ChatCompletionUsage


class TextCompletionRequest(BaseModel):
    model: str
    prompt: str | list[str]
    temperature: float | None = 0.7
    max_tokens: int | None = 1024
    stream: bool | None = False
    top_p: float | None = 1.0
    n: int | None = 1
    stop: str | list[str] | None = None


class TextCompletionChoice(BaseModel):
    index: int
    text: str
    finish_reason: str = "stop"


class TextCompletionResponse(BaseModel):
    id: str
    object: str = "text_completion"
    created: int
    model: str
    choices: list[TextCompletionChoice]
    usage: ChatCompletionUsage


def generate_fake_embedding(text: str, dim: int = EMBEDDING_DIM) -> list[float]:
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
    return 10


def generate_mock_response(messages: list[ChatMessage]) -> str:
    last_message = messages[-1] if messages else None
    if not last_message:
        return "Mock response"

    # Check if system prompt contains numbered sources (RAG context)
    system_msg = next((m for m in messages if m.role == "system"), None)
    has_numbered_sources = system_msg and isinstance(system_msg.content, str) and "[Source 1]" in system_msg.content

    content = last_message.content
    if isinstance(content, list):
        text_parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
        content = " ".join(text_parts) if text_parts else "image analysis request"

    content_lower = str(content).lower()

    if "contextualize" in content_lower or "context" in content_lower:
        response = "This chunk discusses the main topic of the document and provides relevant context for understanding the content."
    elif "describe" in content_lower or "image" in content_lower:
        response = "This is an image showing relevant content from the document."
    elif "summarize" in content_lower:
        response = "This is a summary of the provided content."
    else:
        response = f"Mock response to: {str(content)[:100]}"

    # Append source citations when RAG context has numbered sources
    if has_numbered_sources:
        response += "\n[Sources: 1]"

    return response


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
    data = [EmbeddingData(embedding=generate_fake_embedding(text), index=i) for i, text in enumerate(inputs)]
    return EmbeddingResponse(
        data=data,
        model=request.model,
        usage={"prompt_tokens": len(inputs) * 10, "total_tokens": len(inputs) * 10},
    )


async def stream_chat_completion(request: ChatCompletionRequest):
    """Generate SSE stream for chat completion."""
    response_text = generate_mock_response(request.messages)
    chunk_id = f"chatcmpl-{uuid.uuid4().hex[:8]}"
    created = int(time.time())

    # Role chunk
    role_chunk = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": request.model,
        "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
    }
    yield f"data: {json.dumps(role_chunk)}\n\n"

    # Content chunks — split into words for realistic streaming
    words = response_text.split(" ")
    for i, word in enumerate(words):
        token = word + (" " if i < len(words) - 1 else "")
        content_chunk = {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": request.model,
            "choices": [{"index": 0, "delta": {"content": token}, "finish_reason": None}],
        }
        yield f"data: {json.dumps(content_chunk)}\n\n"

    # Finish chunk
    finish_chunk = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": request.model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    yield f"data: {json.dumps(finish_chunk)}\n\n"

    yield "data: [DONE]\n\n"


def _resolve_ref(schema: dict, root: dict) -> dict:
    """Dereference a $ref against the root schema document."""
    if "$ref" not in schema:
        return schema
    parts = schema["$ref"].lstrip("#/").split("/")
    node = root
    for part in parts:
        node = node.get(part, {})
    return node


def _mock_value(schema: dict, root: dict, user_text: str):
    """Recursively build a mock value that satisfies the given JSON Schema node."""
    schema = _resolve_ref(schema, root)
    # anyOf / oneOf — nullable fields (union with null) return None to avoid invalid mock values
    for combiner in ("anyOf", "oneOf"):
        if combiner in schema:
            branches = schema[combiner]
            has_null = any(s.get("type") == "null" for s in branches)
            if has_null:
                return None
            non_null = [s for s in branches if s.get("type") != "null"]
            if non_null:
                return _mock_value(non_null[0], root, user_text)
            return None
    t = schema.get("type")
    if t == "object" or "properties" in schema:
        return {k: _mock_value(v, root, user_text) for k, v in schema.get("properties", {}).items()}
    if t == "array":
        items = _resolve_ref(schema.get("items", {}), root)
        return [_mock_value(items, root, user_text)]
    if t == "string":
        return user_text
    if t in ("integer", "number"):
        return 0
    if t == "boolean":
        return False
    return None


def generate_tool_call_response(request: ChatCompletionRequest) -> dict:
    """Generate a mock tool_calls response for structured output requests."""
    tool = request.tools[0]
    fn = tool.get("function", tool)
    fn_name = fn.get("name", "unknown")
    parameters = fn.get("parameters", {})
    properties = parameters.get("properties", {})

    # Build mock arguments based on property types
    last_user_msg = next((m for m in reversed(request.messages) if m.role == "user"), None)
    user_text = str(last_user_msg.content)[:100] if last_user_msg else "mock query"

    mock_args: dict = {
        prop_name: _mock_value(prop_schema, parameters, user_text) for prop_name, prop_schema in properties.items()
    }

    prompt_tokens = sum(count_tokens(str(msg.content)) for msg in request.messages)
    args_json = json.dumps(mock_args)
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": request.model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": f"call_{uuid.uuid4().hex[:8]}",
                            "type": "function",
                            "function": {"name": fn_name, "arguments": args_json},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": count_tokens(args_json),
            "total_tokens": prompt_tokens + count_tokens(args_json),
        },
    }


@app.post("/v1/chat/completions")
async def create_chat_completion(request: ChatCompletionRequest):
    """Mock chat completion endpoint for LLM/VLM requests."""
    # Handle function/tool calling (used by structured output)
    if request.tools:
        return generate_tool_call_response(request)

    if request.stream:
        return StreamingResponse(
            stream_chat_completion(request),
            media_type="text/event-stream",
        )

    # Calculate token counts
    prompt_tokens = sum(count_tokens(str(msg.content)) for msg in request.messages)
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
    prompts = request.prompt if isinstance(request.prompt, list) else [request.prompt]
    prompt_tokens = sum(count_tokens(p) for p in prompts)
    response_text = f"Mock completion for: {prompts[0][:50]}..."
    completion_tokens = count_tokens(response_text)
    return TextCompletionResponse(
        id=f"cmpl-{uuid.uuid4().hex[:8]}",
        created=int(time.time()),
        model=request.model,
        choices=[TextCompletionChoice(index=0, text=response_text, finish_reason="stop")],
        usage=ChatCompletionUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
