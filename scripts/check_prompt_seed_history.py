"""Keep the previous bundled prompt hash eligible when its template changes."""

from __future__ import annotations

import ast
import hashlib
import subprocess
import sys
from pathlib import Path

_TYPES_BY_TEMPLATE = {
    "asr_transcription_tmpl.txt": "asr_transcription",
    "chunk_contextualizer_tmpl.txt": "chunk_contextualizer",
    "hyde.txt": "hyde",
    "image_captioning_tmpl.txt": "image_captioning",
    "multi_query_pmpt_tmpl.txt": "multi_query",
    "query_contextualizer_tmpl.txt": "query_contextualizer",
    "spoken_style_answer_tmpl.txt": "spoken_style_answer",
    "sys_prompt_tmpl.txt": "sys_prompt",
    "topic_tagger_tmpl.txt": "topic_tagger",
}


def _registry() -> tuple[dict[str, str], dict[str, set[str]]]:
    source = Path("openrag/services/orchestrators/prompt_seed_hashes.py").read_text()
    assignments = {
        node.target.id: node.value
        for node in ast.parse(source).body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }
    current = ast.literal_eval(assignments["_CURRENT_SEED_HASHES"])
    superseded = {}
    for key, value in zip(
        assignments["_SUPERSEDED_SEED_HASHES"].keys,
        assignments["_SUPERSEDED_SEED_HASHES"].values,
        strict=True,
    ):
        if not isinstance(value, ast.Call) or not value.args:
            raise ValueError("Superseded prompt hashes must be literal frozensets")
        superseded[ast.literal_eval(key)] = set(ast.literal_eval(value.args[0]))
    return current, superseded


def check(base_sha: str) -> list[str]:
    current, superseded = _registry()
    changed = subprocess.check_output(
        ["git", "diff", "--no-renames", "--name-only", base_sha, "--", "openrag/prompts/templates"],
        text=True,
    ).splitlines()
    errors = []
    for path in changed:
        template = Path(path)
        prompt_type = _TYPES_BY_TEMPLATE.get(template.name)
        if prompt_type is None or not template.is_file():
            continue
        old = subprocess.run(["git", "show", f"{base_sha}:{path}"], capture_output=True, check=False)
        if old.returncode != 0:
            continue  # A new template has no older default to preserve.
        old_hash = hashlib.sha256(old.stdout).hexdigest()
        new_hash = hashlib.sha256(template.read_bytes()).hexdigest()
        if old_hash == new_hash:
            continue
        if current.get(prompt_type) != new_hash:
            errors.append(f"{prompt_type}: record the new bundled template hash as current")
        if old_hash not in superseded.get(prompt_type, set()):
            errors.append(f"{prompt_type}: move previous hash {old_hash} into its superseded set")
    return errors


if __name__ == "__main__":
    errors = check(sys.argv[1])
    for error in errors:
        print(error, file=sys.stderr)
    sys.exit(bool(errors))
