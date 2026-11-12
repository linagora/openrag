"""The steps a chat request is answered with, one class per step.

Each one takes what it needs from outside (the LLM, the prompt library,
retrieval, web search, the clock, the language detector) in its constructor,
so it is testable on its own. ``ChatService`` calls them in order today; the
``classic`` graph will call them as nodes, and the ``agentic`` one as tools.

- ``contextualize``: the conversation rewritten into search queries.
- ``scope``: the partitions searched, narrowed by a workspace or attachments.
- ``retrieve``: the documents and the web results, searched in parallel.
- ``web_search``: every query searched on the web, results deduplicated.
- ``summarize``: map-reduce, keeping the relevant chunks as summaries.
"""
