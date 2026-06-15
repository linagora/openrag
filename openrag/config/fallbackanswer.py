# Sent to the client when the LLM produces no usable content (e.g. the whole
# response was a [Sources: ...] tag), so it never receives an empty completion.
EMPTY_RESPONSE_FALLBACK_MESSAGE = "I could not generate an answer based on the retrieved documents"
