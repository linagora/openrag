"""Tests for the generated promptfoo configs."""

from __future__ import annotations

from core.evaluation.promptfoo_config import build_answer_config, build_retrieval_config
from core.models.evaluation import EvalTestCase

CASES = [
    EvalTestCase(query="What is the refund window?", expected_answer="30 days", expected_file_ids=("p.pdf",)),
    EvalTestCase(query="Who approves?", expected_answer="The CFO"),
]

COMMON = {
    "api_base_url": "http://openrag:8080/",
    "partition": "__eval_abc",
    "token": "or-secret",
    "grader_model": "qwen",
    "grader_base_url": "http://vllm:8000/v1",
}


def test_retrieval_provider_targets_the_single_partition_search_route():
    config = build_retrieval_config(cases=CASES, **COMMON, top_k=7)
    url = config["providers"][0]["config"]["url"]
    assert url.startswith("http://openrag:8080/search/partition/__eval_abc")
    assert "top_k=7" in url


def test_retrieval_query_is_url_encoded():
    """A question containing '&' would otherwise truncate the query string."""
    config = build_retrieval_config(cases=CASES, **COMMON)
    assert "{{ query | urlencode }}" in config["providers"][0]["config"]["url"]


def test_retrieval_asserts_on_the_chunk_text():
    config = build_retrieval_config(cases=CASES, **COMMON)
    assertion = config["tests"][0]["assert"][0]
    assert assertion["type"] == "context-relevance"
    assert "d.content" in assertion["contextTransform"]


def test_answer_provider_posts_to_the_partition_scoped_model():
    config = build_answer_config(cases=CASES, **COMMON)
    body = config["providers"][0]["config"]["body"]
    assert config["providers"][0]["config"]["url"] == "http://openrag:8080/v1/chat/completions"
    assert body["model"] == "openrag-__eval_abc"
    assert body["stream"] is False


def test_answer_transform_is_a_single_expression():
    """promptfoo evaluates transformResponse as an expression — a statement or
    an IIFE fails at runtime with a transform error, which manifests as every
    answer scoring zero."""
    transform = build_answer_config(cases=CASES, **COMMON)["providers"][0]["config"]["transformResponse"]
    assert transform == "json.choices[0].message.content"
    assert "return" not in transform
    assert ";" not in transform


def test_answer_grades_against_the_expected_answer():
    config = build_answer_config(cases=CASES, **COMMON)
    types = [assertion["type"] for assertion in config["tests"][0]["assert"]]
    assert types == ["factuality", "llm-rubric"]
    assert config["tests"][0]["assert"][0]["value"] == "{{expected_answer}}"


def test_both_configs_send_the_bearer_token():
    for config in (
        build_retrieval_config(cases=CASES, **COMMON),
        build_answer_config(cases=CASES, **COMMON),
    ):
        headers = config["providers"][0]["config"]["headers"]
        assert headers["Authorization"] == "Bearer or-secret"


def test_grader_points_at_the_configured_openrag_llm():
    """Model-graded assertions must not silently fall back to OpenAI."""
    config = build_answer_config(cases=CASES, **COMMON)
    grader = config["defaultTest"]["options"]["provider"]
    assert grader["id"] == "openai:chat:qwen"
    assert grader["config"]["apiBaseUrl"] == "http://vllm:8000/v1"
    assert grader["config"]["apiKey"]


def test_every_case_becomes_a_test_with_its_vars():
    """Only the vars an assertion actually templates are emitted — the ranking
    metrics read expected_file_ids from the test set, not from promptfoo."""
    config = build_retrieval_config(cases=CASES, **COMMON)
    assert len(config["tests"]) == 2
    assert config["tests"][0]["vars"] == {
        "query": "What is the refund window?",
        "expected_answer": "30 days",
    }


def test_assertions_are_not_shared_between_tests():
    """A shared list would serialise as a YAML anchor plus aliases."""
    tests = build_answer_config(cases=CASES, **COMMON)["tests"]
    assert tests[0]["assert"][0] is not tests[1]["assert"][0]
