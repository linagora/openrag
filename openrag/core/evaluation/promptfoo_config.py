"""Generation of the promptfoo configs a run executes.

A run produces two configs rather than one, because the two questions need
different endpoints:

* **retrieval** hits ``GET /search/partition/{partition}``, whose documents
  carry the chunk ``content`` — the text that ``context-relevance`` grades and
  whose ``metadata.file_id`` feeds hit rate / MRR / recall.
* **answer** hits ``POST /v1/chat/completions``, whose ``extra.sources`` carry
  source metadata but no chunk text, and whose message content is what
  ``factuality`` and ``llm-rubric`` grade.

Keeping them separate means every assertion in a config applies to that
config's single provider, so no assertion ever runs against an output shape it
cannot read.

This module is pure: it returns plain dicts. Serialisation and execution live
in the worker.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from core.models.evaluation import EvalTestCase

#: promptfoo templates with Nunjucks; ``urlencode`` keeps a question
#: containing ``&`` or ``?`` from corrupting the search query string.
_QUERY_TEMPLATE = "{{ query | urlencode }}"

#: Extract the ``documents`` array from the search response.
_SEARCH_TRANSFORM = "json.documents || []"

#: promptfoo evaluates ``transformResponse`` as a single JavaScript
#: *expression* — statements are a syntax error, and an IIFE trips its
#: evaluator — so this extracts the answer text and nothing more. The answer
#: assertions grade that text; retrieved sources come from the retrieval pass.
_CHAT_TRANSFORM = "json.choices[0].message.content"

_RUBRIC = (
    "The response must answer the question using the retrieved documents. "
    "Grade it against this reference answer: {{expected_answer}}. "
    "Pass if the response conveys the same facts, even if worded differently. "
    "Fail if it contradicts the reference, is empty, or refuses to answer."
)


def _grader(model: str, base_url: str, api_key: str | None) -> dict[str, Any]:
    """The provider promptfoo uses for model-graded assertions.

    Points at OpenRAG's own OpenAI-compatible LLM endpoint so an eval needs no
    third-party credentials.
    """
    config: dict[str, Any] = {"apiBaseUrl": base_url}
    # vLLM ignores the key but the OpenAI client refuses to send without one.
    config["apiKey"] = api_key or "sk-no-key-required"
    return {"id": f"openai:chat:{model}", "config": config}


def _tests(cases: Sequence[EvalTestCase], asserts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "vars": {
                "query": case.query,
                "expected_answer": case.expected_answer,
                "expected_file_ids": list(case.expected_file_ids),
            },
            "assert": asserts,
        }
        for case in cases
    ]


def build_retrieval_config(
    *,
    cases: Sequence[EvalTestCase],
    api_base_url: str,
    partition: str,
    token: str,
    grader_model: str,
    grader_base_url: str,
    grader_api_key: str | None = None,
    top_k: int = 5,
    relevance_threshold: float = 0.0,
) -> dict[str, Any]:
    """Config that measures what the retriever returns for each question.

    ``relevance_threshold`` defaults to 0 so ``context-relevance`` records a
    score without failing the run; the deterministic ranking metrics are
    computed from the same responses afterwards.
    """
    url = f"{api_base_url.rstrip('/')}/search/partition/{partition}?text={_QUERY_TEMPLATE}&top_k={top_k}"
    return {
        "description": f"OpenRAG retrieval eval ({partition})",
        "prompts": ["{{query}}"],
        "providers": [
            {
                "id": "https",
                "label": "openrag-retrieval",
                "config": {
                    "url": url,
                    "method": "GET",
                    "headers": {"Authorization": f"Bearer {token}"},
                    "transformResponse": _SEARCH_TRANSFORM,
                },
            }
        ],
        "defaultTest": {"options": {"provider": _grader(grader_model, grader_base_url, grader_api_key)}},
        "tests": _tests(
            cases,
            [
                {
                    "type": "context-relevance",
                    "contextTransform": "output.map(d => d.content).join('\\n\\n')",
                    "threshold": relevance_threshold,
                }
            ],
        ),
    }


def build_answer_config(
    *,
    cases: Sequence[EvalTestCase],
    api_base_url: str,
    partition: str,
    token: str,
    grader_model: str,
    grader_base_url: str,
    grader_api_key: str | None = None,
) -> dict[str, Any]:
    """Config that grades the generated answer against the expected one."""
    return {
        "description": f"OpenRAG answer eval ({partition})",
        "prompts": ["{{query}}"],
        "providers": [
            {
                "id": "https",
                "label": "openrag-chat",
                "config": {
                    "url": f"{api_base_url.rstrip('/')}/v1/chat/completions",
                    "method": "POST",
                    "headers": {
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    "body": {
                        "model": f"openrag-{partition}",
                        "messages": [{"role": "user", "content": "{{query}}"}],
                        "stream": False,
                    },
                    "transformResponse": _CHAT_TRANSFORM,
                },
            }
        ],
        "defaultTest": {"options": {"provider": _grader(grader_model, grader_base_url, grader_api_key)}},
        "tests": _tests(
            cases,
            [
                {"type": "factuality", "value": "{{expected_answer}}"},
                {"type": "llm-rubric", "value": _RUBRIC},
            ],
        ),
    }


__all__ = ["build_answer_config", "build_retrieval_config"]
