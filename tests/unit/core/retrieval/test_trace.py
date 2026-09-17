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
            "prompt": {"content_hash": "hash"},
        },
    }


def test_safe_public_value_projects_nested_contextualization_by_container():
    value = safe_public_value(
        {
            "contextualization": {
                "subqueries": [
                    {
                        "query": "generated query",
                        "temporal_filters": [
                            {
                                "operator": ">=",
                                "value": "2026-01-01T00:00:00+00:00",
                                "content": "private document text",
                            }
                        ],
                        "prompt": {"query": "private prompt"},
                        "embedding": [0.1, 0.2],
                    }
                ],
                "prompt": {
                    "name": "contextualization",
                    "source": "named",
                    "content_hash": "hash",
                    "query": "private prompt",
                    "original_query": "private user message copied into prompt",
                    "embedding": [0.3, 0.4],
                },
            }
        }
    )

    assert value == {
        "contextualization": {
            "subqueries": [
                {
                    "query": "generated query",
                    "temporal_filters": [{"operator": ">=", "value": "2026-01-01T00:00:00+00:00"}],
                }
            ],
            "prompt": {
                "name": "contextualization",
                "source": "named",
                "content_hash": "hash",
            },
        }
    }


@pytest.mark.parametrize(
    "values",
    [
        {"prompt": {"content_hash": "hash", "query": "private prompt"}},
        {"subqueries": [{"query": "public query", "embedding": [0.1]}]},
    ],
)
def test_contextualization_nested_models_reject_non_contract_fields(values):
    with pytest.raises(ValidationError):
        ContextualizationTrace(**values)


def test_trace_error_messages_are_replaced_with_safe_metadata():
    sensitive = (
        "Authorization: Bearer bearer-secret api_key=key-secret "
        "https://user:password@example.test/path?token=url-secret "
        "PRIVATE DOCUMENT EXCERPT"
    )
    builder = RetrievalTraceBuilder("request", "query")
    builder.record_stage("final", status="error", candidates=[], error=sensitive)
    builder.record_error("final", RuntimeError(sensitive))
    builder.contextualization = ContextualizationTrace(
        error=TraceError(stage="contextualization", kind="parse_error", message=sensitive)
    )

    trace = builder.finish(configuration_fingerprint="fingerprint")

    final_stage = next(stage for stage in trace["stages"] if stage["name"] == "final")
    assert final_stage["error"] == "redacted"
    assert trace["errors"] == [{"stage": "final", "message": "redacted", "kind": "RuntimeError"}]
    assert trace["contextualization"]["error"] == {
        "stage": "contextualization",
        "message": "redacted",
        "kind": "parse_error",
    }
    serialized = json.dumps(trace)
    for prohibited in (
        "Authorization",
        "Bearer",
        "bearer-secret",
        "api_key",
        "key-secret",
        "user:password",
        "url-secret",
        "PRIVATE DOCUMENT EXCERPT",
    ):
        assert prohibited not in serialized


def test_safe_public_value_omits_internal_endpoint_urls():
    assert safe_public_value({"endpoint": "https://internal-llm.example.test/v1"}) == {}


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


def test_trace_errors_are_safe_and_unvisited_stages_are_explicit():
    builder = RetrievalTraceBuilder("request", "query")
    builder.record_error("dense_before_threshold", RuntimeError("x" * 600))

    trace = builder.finish(configuration_fingerprint="fingerprint")

    assert trace["schema_version"] == TRACE_SCHEMA_VERSION == 1
    assert [stage["name"] for stage in trace["stages"]] == list(TRACE_STAGE_NAMES)
    assert {stage["status"] for stage in trace["stages"]} == {"not_run"}
    assert trace["errors"][0] == {
        "stage": "dense_before_threshold",
        "message": "redacted",
        "kind": "RuntimeError",
    }
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


def test_trace_stage_caps_serialized_candidates_but_keeps_true_count():
    builder = RetrievalTraceBuilder("request", "query")
    candidates = [TraceCandidate(id=f"chunk-{index}", rank=index + 1) for index in range(201)]

    builder.record_stage("final", status="complete", candidates=candidates)
    trace = builder.finish(configuration_fingerprint="fingerprint")

    final_stage = next(stage for stage in trace["stages"] if stage["name"] == "final")
    assert final_stage["candidate_count"] == 201
    assert len(final_stage["candidates"]) == 200
    assert final_stage["candidates"][-1]["id"] == "chunk-199"
