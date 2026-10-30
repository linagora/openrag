"""Shared output and intent guidance appended to contextualizer prompts."""

from core.models.query import MAX_QUERY_SUBQUERIES

QUERY_CONTEXTUALIZER_JSON_HINT = (
    "\n\nClassify the full latest user message. Retrieve for factual, informational, analytical, mixed, or uncertain "
    "content, even without a question mark or alongside a social phrase. Skip retrieval only for an exclusively "
    "social greeting, thanks, farewell, or capability question. A brief acknowledgement stays social unless it "
    "accepts a prior offer of factual or document-backed continuation; replies to social, wellbeing, or feedback "
    "questions stay social. Return only SearchQueries JSON: "
    '{"intent":"other","requires_retrieval":true,"query_list":[{"query":"...","temporal_filters":null}]}. '
    'For an exclusively social message, use its matching intent ("greeting", "gratitude", "farewell", or '
    '"capability"), false, and an empty list. '
    f"query_list may contain one or more distinct sub-queries, up to {MAX_QUERY_SUBQUERIES}, when the template "
    "rules require a split. If uncertain, retrieve."
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
        " At this budget, combine items with identical date filters; split only for different date constraints. "
        "Keep every item and the complete date interval."
        if output_tokens <= 512
        else ""
    )
    return (
        f"\n\nFit the complete JSON within {output_tokens} output tokens and emit at most {query_budget} sub-queries. "
        "This budget takes precedence over one-query-per-item comparisons when needed. Group related items or "
        "adjacent periods while retaining every item, shared criterion, and date filter. Use concise queries and "
        "omit default fields (intent other, requires_retrieval true, field created_at)." + small_budget
    )
