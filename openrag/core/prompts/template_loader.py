"""Disk-based prompt template loader.

Pure I/O helper: given a directory and a filename, read and return the file
contents as a string. Callers (typically the DI/composition layer) resolve
the directory and the filename mapping from config; this function has no
config dependency of its own.
"""

from __future__ import annotations

from pathlib import Path


def load_template(prompts_dir: str | Path, file_name: str) -> str:
    """Read a prompt template from disk.

    Args:
        prompts_dir: Directory containing prompt template files.
        file_name: Template filename (relative to ``prompts_dir``).

    Returns:
        The template contents as a string.

    Raises:
        FileNotFoundError: if the resolved path does not exist.
    """
    base = Path(prompts_dir).resolve()
    file_path = (base / file_name).resolve()
    if not file_path.is_relative_to(base):
        raise ValueError(f"Prompt path escapes base directory: `{file_name}`")
    if not file_path.exists():
        raise FileNotFoundError(f"Prompt file not found: `{file_path}`")
    return file_path.read_text(encoding="utf-8")


def load_template_by_key(
    prompts_dir: str | Path,
    prompt_mapping: object,
    prompt_key: str,
) -> str:
    """Read a prompt by logical key, looking up the filename on a mapping object.

    The mapping object is typically the ``PromptsConfig`` Pydantic model with
    attributes like ``sys_prompt``, ``hyde``, ``multi_query`` whose values are
    template filenames.

    Args:
        prompts_dir: Directory containing prompt template files.
        prompt_mapping: Object exposing prompt keys as attributes.
        prompt_key: Attribute name on ``prompt_mapping`` (e.g. ``"hyde"``).

    Returns:
        The template contents as a string.

    Raises:
        ValueError: if ``prompt_key`` is not defined on ``prompt_mapping``.
        FileNotFoundError: if the resolved path does not exist.
    """
    file_name = getattr(prompt_mapping, prompt_key, None)
    if not file_name:
        raise ValueError(f"No associated file name found for prompt: `{prompt_key}`")
    return load_template(prompts_dir, file_name)
