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
    ContextualizedSubqueryTrace,
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
    merge_child_traces,
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


def test_contextualization_bypass_is_preserved_by_safe_serialization():
    builder = RetrievalTraceBuilder("request", "exact original query")
    builder.contextualization = ContextualizationTrace(
        original_query="exact original query",
        bypassed=True,
    )

    trace = builder.finish(configuration_fingerprint="fingerprint")

    assert trace["contextualization"]["bypassed"] is True


@pytest.mark.parametrize(
    ("query", "secret"),
    [
        ("Authorization: Bearer bearer-secret", "bearer-secret"),
        ("Authorization: Basic basic-secret", "basic-secret"),
        ("Use Bearer bare-secret for the request", "bare-secret"),
        ('Find token="quoted-secret" in the policy', "quoted-secret"),
        ('Find token="multiline-secret\ncontinued-secret" in the policy', "multiline-secret"),
        ("Find api_key='quoted-key' in the policy", "quoted-key"),
    ],
)
def test_trace_query_fields_scrub_embedded_credentials(query, secret):
    builder = RetrievalTraceBuilder("request", query)
    builder.contextualization = ContextualizationTrace(
        original_query=query,
        subqueries=[ContextualizedSubqueryTrace(query=query)],
    )
    child = RetrievalTraceBuilder("child", query)
    builder.record_query_trace(child)

    trace = builder.finish(configuration_fingerprint="fingerprint")

    serialized = json.dumps(trace)
    assert secret not in serialized
    assert trace["original_query"] != query
    assert trace["contextualization"]["original_query"] != query
    assert trace["contextualization"]["subqueries"][0]["query"] != query
    assert trace["query_traces"][0]["query"] != query


@pytest.mark.parametrize(
    "query",
    [
        "What is the basic idea of RAG?",
        "Explain basic authentication",
        "How do I reset my password: step by step?",
        "What does the token: limit mean for the API?",
        "Bearer bonds vs stocks",
    ],
)
def test_trace_preserves_natural_language_that_mentions_authentication(query):
    trace = RetrievalTraceBuilder("request", query).finish(configuration_fingerprint="fingerprint")

    assert trace["original_query"] == query


@pytest.mark.parametrize(
    "query",
    [
        "What is basic authentication?",
        "Explain basic concepts.",
        "What does token: limit mean?",
    ],
)
def test_trace_preserves_sentence_punctuation_after_non_credentials(query):
    trace = RetrievalTraceBuilder("request", query).finish(configuration_fingerprint="fingerprint")

    assert trace["original_query"] == query


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


def test_safe_public_value_is_deny_by_default_at_every_trace_boundary():
    value = safe_public_value(
        {
            "chunk_text": "private root text",
            "embedding": [0.1, 0.2],
            "stages": [
                {
                    "name": "dense_after_threshold",
                    "status": "complete",
                    "text": "private stage text",
                    "embedding": [0.3, 0.4],
                    "candidates": [
                        {
                            "id": "chunk-1",
                            "rank": 1,
                            "text": "private candidate text",
                            "content": "private candidate content",
                            "metadata": {"source": "private source"},
                            "scores": {"dense": 0.9, "snippet": "private score text"},
                        }
                    ],
                }
            ],
            "comparisons": {
                "original_query": {
                    "status": "complete",
                    "unexpected": {"original_query": "private unknown context"},
                    "errors": [{"stage": "search", "message": "10.0.0.5:19530 failed"}],
                }
            },
        }
    )

    assert value == {
        "stages": [
            {
                "name": "dense_after_threshold",
                "status": "complete",
                "candidates": [{"id": "chunk-1", "rank": 1, "scores": {"dense": 0.9}}],
            }
        ],
        "comparisons": {
            "original_query": {
                "status": "complete",
                "errors": [{"stage": "search", "message": "redacted"}],
            }
        },
    }


@pytest.mark.parametrize("non_finite", [float("nan"), float("inf"), float("-inf")])
def test_safe_public_value_omits_non_finite_scores(non_finite):
    value = safe_public_value(
        {
            "stages": [
                {
                    "name": "post_rerank",
                    "status": "complete",
                    "candidates": [
                        {
                            "id": "chunk",
                            "rank": 1,
                            "scores": {"reranker": non_finite, "dense": 0.5},
                        }
                    ],
                }
            ]
        }
    )

    assert value["stages"][0]["candidates"][0]["scores"] == {"dense": 0.5}
    json.dumps(value, allow_nan=False)


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


def test_query_traces_preserve_each_subquery_after_parent_candidate_cap():
    parent = RetrievalTraceBuilder("request", "question")
    for query_index in range(2):
        child = RetrievalTraceBuilder(f"child-{query_index}", f"query-{query_index}")
        child.record_stage(
            "dense_after_threshold",
            status="complete",
            candidates=[
                TraceCandidate(id=f"query-{query_index}-chunk-{candidate_index}", rank=candidate_index + 1)
                for candidate_index in range(150)
            ],
        )
        parent.record_query_trace(child)

    trace = parent.finish(configuration_fingerprint="fingerprint")

    assert [query_trace["query"] for query_trace in trace["query_traces"]] == ["query-0", "query-1"]
    assert [
        len(next(stage for stage in query_trace["stages"] if stage["name"] == "dense_after_threshold")["candidates"])
        for query_trace in trace["query_traces"]
    ] == [150, 150]
    candidate = next(stage for stage in trace["query_traces"][0]["stages"] if stage["name"] == "dense_after_threshold")[
        "candidates"
    ][0]
    assert set(candidate) == {
        "id",
        "document_id",
        "rank",
        "scores",
        "duplicate_of",
        "removal_reason",
    }


def test_merge_child_traces_preserves_partition_and_nested_query_stages():
    request = RetrievalTraceBuilder("request", "follow-up question")
    partition = RetrievalTraceBuilder("partition", None, partition="legal")
    effective_query = RetrievalTraceBuilder("effective", "contextualized question")
    generated_query = RetrievalTraceBuilder("generated", "generated variant")
    generated_query.record_stage(
        "dense_after_threshold",
        status="complete",
        candidates=[TraceCandidate(id="gold", rank=1)],
    )
    effective_query.record_query_trace(generated_query)
    partition.record_query_trace(effective_query)

    merge_child_traces(request, [partition])
    trace = request.finish(configuration_fingerprint="fingerprint")

    partition_trace = trace["query_traces"][0]
    assert partition_trace["partition"] == "legal"
    assert partition_trace["query"] is None
    effective_trace = partition_trace["query_traces"][0]
    assert effective_trace["query"] == "contextualized question"
    generated_trace = effective_trace["query_traces"][0]
    assert generated_trace["query"] == "generated variant"
    dense = next(stage for stage in generated_trace["stages"] if stage["name"] == "dense_after_threshold")
    assert dense["candidates"][0]["id"] == "gold"
    assert request.stages["dense_after_threshold"].status == "unavailable"
