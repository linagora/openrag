from pathlib import Path

from config import load_config

config = load_config()

prompts_dir: Path = config.paths.prompts_dir
prompt_mapping: dict = config.prompts


def load_prompt(
    prompt_name: str,
    prompts_dir: Path = prompts_dir,
    prompt_mapping: dict = prompt_mapping,
) -> str:
    file_name = prompt_mapping.get(prompt_name, None)
    if not file_name:
        raise ValueError(f"No associated file name found for prompt: `{prompt_name}`")

    file_path = prompts_dir / file_name

    if not file_path.exists():
        raise FileNotFoundError(f"Prompt file not found: `{file_path}`")

    with open(file_path, mode="r") as f:
        sys_msg = f.read()
        return sys_msg


# Load prompts
SYS_PROMPT_TMPLT = load_prompt("sys_prompt")
QUERY_CONTEXTUALIZER_PROMPT = load_prompt("query_contextualizer")
CHUNK_CONTEXTUALIZER_PROMPT = load_prompt("chunk_contextualizer")
IMAGE_DESCRIBER = load_prompt("image_describer")

# Retrievers prompts
HYDE_PROMPT = load_prompt("hyde")
MULTI_QUERY_PROMPT = load_prompt("multi_query")

# Short answer prompt
SPOKEN_STYLE_ANSWER_PROMPT = load_prompt("spoken_style_answer")
