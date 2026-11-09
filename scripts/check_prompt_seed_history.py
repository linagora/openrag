"""Require an allow-list entry when a shipped prompt template changes."""

from __future__ import annotations

import hashlib
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_REGISTRY_PATH = _REPOSITORY_ROOT / "openrag/services/orchestrators/prompt_seed_hashes.py"
_STABLE_RELEASE_TAG = re.compile(r"^v\d+\.\d+\.\d+$")
# The prompt library first shipped in v2.1.0; earlier prompt files could not
# have seeded rows in the prompts table.
_MIN_PROMPT_LIBRARY_RELEASE = (2, 1, 0)


def _load_seed_registry():
    spec = importlib.util.spec_from_file_location("prompt_seed_hashes", _REGISTRY_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load prompt seed registry at {_REGISTRY_PATH}")
    registry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(registry)
    return registry


_PROMPT_SEEDS = _load_seed_registry()
_TEMPLATE_TO_TYPE = {
    filename: prompt_type for prompt_type, (_, filename) in _PROMPT_SEEDS._PROMPT_TEMPLATE_METADATA.items()
}


def _stable_release_tags() -> list[str]:
    tags = subprocess.check_output(["git", "tag", "--list"], text=True).splitlines()
    releases = []
    for tag in tags:
        if not _STABLE_RELEASE_TAG.fullmatch(tag):
            continue
        version = tuple(int(part) for part in tag[1:].split("."))
        if version >= _MIN_PROMPT_LIBRARY_RELEASE:
            releases.append(tag)
    return releases


def _released_hashes(path: str, tags: list[str]) -> set[str]:
    hashes = set()
    for tag in tags:
        result = subprocess.run(["git", "show", f"{tag}:{path}"], capture_output=True, check=False)
        if result.returncode == 0:
            hashes.add(hashlib.sha256(result.stdout).hexdigest())
    return hashes


def _registry() -> dict[str, tuple[str, ...]]:
    return _PROMPT_SEEDS._PROMPT_SEED_HASH_HISTORY


def check(base_sha: str) -> list[str]:
    history_by_type = _registry()
    tags = _stable_release_tags()
    changed = subprocess.check_output(
        ["git", "diff", "--no-renames", "--name-only", base_sha, "--", "openrag/prompts/templates"],
        text=True,
    ).splitlines()
    errors = []
    for path in changed:
        template = Path(path)
        prompt_type = _TEMPLATE_TO_TYPE.get(template.name)
        if prompt_type is None or not template.is_file():
            continue
        old = subprocess.run(["git", "show", f"{base_sha}:{path}"], capture_output=True, check=False)
        if old.returncode != 0:
            continue  # A new template has no older default to preserve.
        old_hash = hashlib.sha256(old.stdout).hexdigest()
        new_hash = hashlib.sha256(template.read_bytes()).hexdigest()
        history = history_by_type.get(prompt_type, ())
        if old_hash == new_hash:
            continue
        if old_hash not in _released_hashes(path, tags):
            continue  # Unreleased development text cannot be a stable upgrade seed.
        if old_hash not in history[:-1]:
            errors.append(f"{prompt_type}: retain released hash {old_hash} in its seed history")
    return errors


if __name__ == "__main__":
    errors = check(sys.argv[1])
    for error in errors:
        print(error, file=sys.stderr)
    sys.exit(bool(errors))
