from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

QueryMode = Literal["contextualized", "original", "compare"]
MAX_DIAGNOSTIC_MESSAGES = 100
MAX_DIAGNOSTIC_INPUT_CHARS = 1_000_000


class RetrievalDiagnosticMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant", "system", "developer"]
    content: str = Field(min_length=1, max_length=MAX_DIAGNOSTIC_INPUT_CHARS)


class RetrievalDiagnosticRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messages: list[RetrievalDiagnosticMessage] = Field(min_length=1, max_length=MAX_DIAGNOSTIC_MESSAGES)
    query_mode: QueryMode = "contextualized"
    top_k: int | None = Field(None, ge=1, le=1000)
    similarity_threshold: float | None = Field(None, ge=0, le=1)
    disable_reranker: bool = False
    disable_expansion: bool = False

    @model_validator(mode="after")
    def validate_latest_message(self):
        if self.messages[-1].role != "user" or not self.messages[-1].content.strip():
            raise ValueError("The latest message must be a non-empty user message")
        if sum(len(message.content) for message in self.messages) > MAX_DIAGNOSTIC_INPUT_CHARS:
            raise ValueError("Diagnostic message content is too large")
        return self


class RetrievalDiagnosticResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_status: Literal["experimental"] = "experimental"
    partition: str
    query_mode: QueryMode
    retrieval_trace: dict[str, object]
