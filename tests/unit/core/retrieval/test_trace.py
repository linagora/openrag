"""Contract tests for version 1 retrieval traces."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from core.models.chunk import Chunk
from core.models.retrieval_result import ScoredChunk
from core.models.retrieval_trace import (
    ContextualizationTrace,
    TraceCandidate,
    TraceError,
    TraceStage,
)
from core.retrieval.trace import (
    REMOVAL_REASONS,
    TRACE_SCHEMA_VERSION,
    TRACE_STAGE_NAMES,
    TRACE_STATUSES,
    RetrievalTraceBuilder,
    candidates_from_chunks,
    canonical_fingerprint,
    safe_public_value,
)
from pydantic import ValidationError

FIXTURE = Path(__file__).parents[3] / "fixtures" / "retrieval_trace_v1.json"


def test_trace_v1_matches_public_fixture():
    builder = RetrievalTraceBuilder("req-1", "What changed?")
    builder.record_stage("original_query", status="complete", candidates=[])
    builder.record_stage(
        "dense_after_threshold",
        status="complete",
        candidates=[
            TraceCandidate(
                id="chunk-1",
                document_id="document-1",
                rank=1,
                scores={"dense": 0.75},
            )
        ],
        duration_seconds=0.012,
    )
    trace = builder.finish(configuration_fingerprint="abc123")
    assert trace == json.loads(FIXTURE.read_text())


def test_safe_public_value_redacts_secrets_and_content():
    value = safe_public_value(
        {
            "api_key": "secret",
            "content": "document",
            "model": "public",
            "contextualization": {
                "model": "also-public",
                "endpoint": "https://user:password@example.test/v1?api_key=secret",
                "authorization": "Bearer secret",
                "prompt": {"content": "private prompt", "content_hash": "hash"},
            },
        }
    )
    assert value == {
        "model": "public",
        "contextualization": {
            "model": "also-public",
            "endpoint": "https://example.test/v1",
            "prompt": {"content_hash": "hash"},
        },
    }


def test_canonical_fingerprint_is_order_independent():
    expected = hashlib.sha256(b'{"a":1,"b":2}').hexdigest()
    assert canonical_fingerprint({"b": 2, "a": 1}) == expected
    assert canonical_fingerprint({"b": 2, "a": 1}) == canonical_fingerprint({"a": 1, "b": 2})


def test_candidates_from_chunks_keeps_only_identifiers_ranks_and_public_scores():
    plain = Chunk(
        id="plain",
        document_id="doc-1",
        text="private document body",
        embedding=[0.1, 0.2],
        metadata={"api_key": "secret"},
    )
    scored = ScoredChunk.from_chunk(
        Chunk(id="scored", document_id="doc-2", text="another private body"),
        vector_score=0.9,
        rerank_score=0.8,
        combined_score=0.7,
    )

    assert [candidate.model_dump() for candidate in candidates_from_chunks([plain, scored])] == [
        {
            "id": "plain",
            "document_id": "doc-1",
            "rank": 1,
            "scores": {},
            "duplicate_of": None,
            "removal_reason": None,
        },
        {
            "id": "scored",
            "document_id": "doc-2",
            "rank": 2,
            "scores": {"dense": 0.9, "fused": 0.7, "reranker": 0.8},
            "duplicate_of": None,
            "removal_reason": None,
        },
    ]


@pytest.mark.parametrize(
    ("model", "values"),
    [
        (TraceCandidate, {"id": "chunk", "rank": 1}),
        (TraceStage, {"name": "final", "status": "complete"}),
        (TraceError, {"stage": "final", "message": "failed"}),
        (ContextualizationTrace, {}),
    ],
)
def test_trace_models_forbid_unknown_fields(model, values):
    with pytest.raises(ValidationError):
        model(**values, private_content="must not be accepted")


def test_trace_errors_are_bounded_and_unvisited_stages_are_explicit():
    builder = RetrievalTraceBuilder("request", "query")
    builder.record_error("dense_before_threshold", RuntimeError("x" * 600))

    trace = builder.finish(configuration_fingerprint="fingerprint")

    assert trace["schema_version"] == TRACE_SCHEMA_VERSION == 1
    assert [stage["name"] for stage in trace["stages"]] == list(TRACE_STAGE_NAMES)
    assert {stage["status"] for stage in trace["stages"]} == {"not_run"}
    assert len(trace["errors"][0]["message"]) == 500
    assert TRACE_STATUSES == ("complete", "not_run", "unavailable", "error")
    assert REMOVAL_REASONS == (
        "dense_threshold",
        "hybrid_top_k",
        "reranker_top_n",
        "final_top_n",
        "duplicate",
        "partition_filter",
        "workspace_filter",
        "attachment_filter",
        "temporal_filter",
    )
