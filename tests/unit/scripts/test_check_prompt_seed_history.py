from __future__ import annotations

import hashlib
import importlib.util
import subprocess
from pathlib import Path

from services.orchestrators.prompt_seed_hashes import _PROMPT_SEED_HASH_HISTORY, _PROMPT_TEMPLATE_METADATA

_SPEC = importlib.util.spec_from_file_location(
    "check_prompt_seed_history", Path(__file__).parents[3] / "scripts/check_prompt_seed_history.py"
)
assert _SPEC is not None and _SPEC.loader is not None
guard = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(guard)


def test_history_check_uses_the_shared_prompt_template_registry():
    assert guard._TEMPLATE_TO_TYPE == {
        filename: prompt_type for prompt_type, (_, filename) in _PROMPT_TEMPLATE_METADATA.items()
    }
    assert set(_PROMPT_SEED_HASH_HISTORY) == set(_PROMPT_TEMPLATE_METADATA)


def test_changed_released_template_retains_old_hash_in_history(monkeypatch, tmp_path):
    template = tmp_path / "openrag/prompts/templates/query_contextualizer_tmpl.txt"
    template.parent.mkdir(parents=True)
    template.write_text("old")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "base",
        ],
        check=True,
    )
    base = subprocess.check_output(["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True).strip()
    subprocess.run(["git", "-C", str(tmp_path), "tag", "v2.3.1", base], check=True)
    template.write_text("new")
    monkeypatch.chdir(tmp_path)
    old_hash = hashlib.sha256(b"old").hexdigest()
    history = {"query_contextualizer": (hashlib.sha256(b"new").hexdigest(),)}
    monkeypatch.setattr(guard, "_registry", lambda: history)

    assert guard.check(base) == [f"query_contextualizer: retain released hash {old_hash} in its seed history"]
    history["query_contextualizer"] = (old_hash, hashlib.sha256(b"new").hexdigest())
    assert guard.check(base) == []


def test_changed_pre_library_template_does_not_expand_supported_history(monkeypatch, tmp_path):
    template = tmp_path / "openrag/prompts/templates/query_contextualizer_tmpl.txt"
    template.parent.mkdir(parents=True)
    template.write_text("development text")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "base",
        ],
        check=True,
    )
    base = subprocess.check_output(["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True).strip()
    subprocess.run(["git", "-C", str(tmp_path), "tag", "v2.0.1", base], check=True)
    template.write_text("new development text")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        guard, "_registry", lambda: {"query_contextualizer": (hashlib.sha256(b"new development text").hexdigest(),)}
    )

    assert guard.check(base) == []
