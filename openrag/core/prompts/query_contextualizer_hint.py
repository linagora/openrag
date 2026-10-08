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
