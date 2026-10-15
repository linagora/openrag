"""Shared output and intent guidance appended to contextualizer prompts."""

from core.models.query import MAX_QUERY_SUBQUERIES

QUERY_CONTEXTUALIZER_JSON_HINT = (
    "\n\nClassify the complete latest user message conservatively. Retrieval is the default. "
    "Only a message consisting exclusively of a greeting or salutation, gratitude, farewell, or a question about "
    "the assistant's capabilities may skip retrieval. A factual, informational, analytical, ambiguous, or mixed "
    "message must require retrieval, even when it has no question mark or also contains a social phrase. "
    "A brief acknowledgement after a social, wellbeing, or feedback question remains gratitude and may skip "
    "retrieval. It requires retrieval only when it accepts factual, informational, analytical, or document-backed "
    "continuation from the assistant's previous turn. "
    "Respond ONLY with one of these JSON forms: "
    '{"intent": "greeting", "requires_retrieval": false, "query_list": []} for an exclusively casual message '
    "(using gratitude, farewell, or capability instead of greeting when appropriate), or "
    '{"intent": "other", "requires_retrieval": true, '
    '"query_list": [{"query": "<search query>", "temporal_filters": null}]} for everything else. '
    f"For retrieval, query_list may contain one or more distinct sub-queries, up to {MAX_QUERY_SUBQUERIES}, "
    "when the contextualizer rules require a split. "
    "If uncertain, use intent other and require retrieval."
)

QUERY_CONTEXTUALIZER_OVER_CAP_RETRY_INSTRUCTION = (
    f"The previous JSON was rejected because it contained more than {MAX_QUERY_SUBQUERIES} sub-queries. "
    "Regenerate the complete result within the cap. For time ranges, group adjacent periods into coarser "
    "contiguous intervals that cover the entire range. For other facets, group related details while retaining "
    "every item and shared criterion. Never omit trailing periods or any item. Return only the full JSON."
)


QUERY_CONTEXTUALIZER_TRUNCATED_RETRY_INSTRUCTION = (
    "The previous response was truncated or invalid JSON. Return a complete compact JSON object. "
    "Return at most two shorter queries; group adjacent time periods or related facets if necessary; "
    "retain every item and shared criterion and preserve all required temporal filters. "
    "Never return a partial list or omit later periods."
)


def contextualizer_budget_hint(output_tokens: int) -> str:
    # Reserve roughly 128 tokens per query for text and a pair of date predicates.
    # This is a planning bound, not a tokenizer-independent fit guarantee.
    query_budget = max(1, min(MAX_QUERY_SUBQUERIES, output_tokens // 128))
    small_budget = (
        " With this small budget, prefer ONE combined query naming ALL requested items and criteria "
        "when their date filters are identical. Only split when different date constraints require it. "
        "For example, comparing A, B, C, D, E, F, G and H must name all eight in the combined query; "
        "returning queries for only A, B, C and D is invalid. Keep the complete date interval. "
        if output_tokens <= 512
        else ""
    )
    return (
        f"\n\nThe complete JSON must fit within {output_tokens} output tokens. "
        f"For this budget, emit at most {query_budget} sub-queries. This budget takes precedence over "
        "the one-query-per-item comparison rule: group items when necessary. "
        "The query cap is a maximum, not a target. Use concise query strings and compact JSON. "
        "For a small budget, group related facets or adjacent periods into fewer queries while retaining "
        "every item and shared criterion and all temporal filters. Do not repeat optional field names "
        "whose defaults already apply (intent other, requires_retrieval true, field created_at)." + small_budget
    )
