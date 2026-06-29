from typing import Literal

from pydantic import BaseModel, Field


class CompletionEvaluationResponse(BaseModel):
    score: int = Field(..., ge=1, le=10, description="Completeness score 1-10.")


class PrecisionEvaluationResponse(BaseModel):
    score: int = Field(..., ge=1, le=10, description="Precision score 1-10.")


class CompletionEvaluationCoTResponse(BaseModel):
    reasoning: str = Field(
        ...,
        description="2-4 phrases expliquant le score : points clés couverts vs omis, et leur importance.",
        max_length=900,
    )
    score: int = Field(..., ge=1, le=10, description="Score d'exhaustivité 1-10.")


class PrecisionEvaluationCoTResponse(BaseModel):
    reasoning: str = Field(
        ...,
        description="2-4 phrases expliquant le score : informations exactes vs fausses/hors sujet, et leur impact.",
        max_length=900,
    )
    score: int = Field(..., ge=1, le=10, description="Score de précision 1-10.")


class CompletionEvaluationCoTResponseEN(BaseModel):
    reasoning: str = Field(
        ...,
        description="2-4 sentences explaining the score: which key points are covered vs omitted, and why it matters.",
        max_length=900,
    )
    score: int = Field(..., ge=1, le=10, description="Completeness score 1-10.")


class PrecisionEvaluationCoTResponseEN(BaseModel):
    reasoning: str = Field(
        ...,
        description="2-4 sentences explaining the score: which facts are accurate vs false/off-topic, and the impact.",
        max_length=900,
    )
    score: int = Field(..., ge=1, le=10, description="Precision score 1-10.")


class ClaimVerdict(BaseModel):
    claim: str = Field(
        ...,
        description="One atomic factual claim, max 15 words, single line, no newlines/tabs/quotes/control chars.",
        max_length=120,
    )
    verdict: Literal["supported", "unsupported"]


class FaithfulnessEvaluationResponse(BaseModel):
    claims: list[ClaimVerdict] = Field(
        default_factory=list,
        description="At most 6 atomic claims with verdicts. Empty if refusal.",
        max_length=6,
    )


class RefusalEvaluationResponse(BaseModel):
    verdict: Literal["refusal", "non_refusal"]


class RelevancyVerdict(BaseModel):
    statement: str = Field(
        ...,
        description="One atomic statement, max 25 words, single line, no newlines/tabs/quotes/control chars.",
        max_length=200,
    )
    verdict: Literal["relevant", "irrelevant"]


class AnswerRelevancyEvaluationResponse(BaseModel):
    """Statements extracted from the answer, each judged relevant/irrelevant to the question."""

    statements: list[RelevancyVerdict] = Field(
        default_factory=list,
        description="Atomic statements from the answer with verdicts. Empty if refusal/no content.",
        max_length=12,
    )


class ContextRelevancyEvaluationResponse(BaseModel):
    """Statements extracted from the retrieval context, each judged relevant/irrelevant to the question."""

    statements: list[RelevancyVerdict] = Field(
        default_factory=list,
        description="Atomic statements from the context with verdicts. Empty if no content.",
        max_length=30,
    )


RESPONSE_LABELS = ("Fully Correct", "Incomplete", "Contradictory")


class ResponseLabelResponse(BaseModel):
    reasoning: str = Field(
        ...,
        description="One concise sentence (max ~25 words) naming the specific gap or contradiction.",
        max_length=200,
    )
    label: Literal["Fully Correct", "Incomplete", "Contradictory"]
