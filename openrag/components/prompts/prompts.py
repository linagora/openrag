"""Backward-compatibility shim — delegates to `openrag.core.prompts.template_loader`.

The disk-based template loader moved to
`openrag/core/prompts/template_loader.py` in Phase 5C. This module is
retained for legacy imports of `load_prompt(...)` and the eagerly-loaded
SYS_PROMPT_TMPLT / *_PROMPT constants until consumers migrate;
scheduled for removal in Phase 12.

The new function takes (prompts_dir, mapping, key) explicitly; this
shim's `load_prompt(key)` resolves the first two from the cached
config, matching the legacy call site shape.
"""

from pathlib import Path

from config import load_config

from openrag.core.prompts.template_loader import load_template_by_key

config = load_config()

prompts_dir: Path = config.paths.prompts_dir
prompt_mapping = config.prompts


def load_prompt(
    prompt_name: str,
    prompts_dir: Path = prompts_dir,
    prompt_mapping=prompt_mapping,
) -> str:
    return load_template_by_key(prompts_dir, prompt_mapping, prompt_name)


# Eagerly-loaded prompt strings — preserved for legacy callers that
# import these names directly. New code should call `load_template_by_key`
# (or `load_template`) on demand instead.
SYS_PROMPT_TMPLT = load_prompt("sys_prompt")
QUERY_CONTEXTUALIZER_PROMPT = load_prompt("query_contextualizer")
CHUNK_CONTEXTUALIZER_PROMPT = load_prompt("chunk_contextualizer")
IMAGE_DESCRIBER = load_prompt("image_describer")

HYDE_PROMPT = load_prompt("hyde")
MULTI_QUERY_PROMPT = load_prompt("multi_query")

SPOKEN_STYLE_ANSWER_PROMPT = load_prompt("spoken_style_answer")
