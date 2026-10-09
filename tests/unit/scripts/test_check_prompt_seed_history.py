from __future__ import annotations

import hashlib
import importlib.util
import subprocess
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "check_prompt_seed_history", Path(__file__).parents[3] / "scripts/check_prompt_seed_history.py"
)
assert _SPEC is not None and _SPEC.loader is not None
guard = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(guard)


def test_changed_template_requires_old_hash_in_superseded_set(monkeypatch, tmp_path):
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
    template.write_text("new")
    monkeypatch.chdir(tmp_path)
    old_hash = hashlib.sha256(b"old").hexdigest()
    new_hash = hashlib.sha256(b"new").hexdigest()
    superseded = {"query_contextualizer": set()}
    monkeypatch.setattr(guard, "_registry", lambda: ({"query_contextualizer": new_hash}, superseded))

    assert guard.check(base) == [f"query_contextualizer: move previous hash {old_hash} into its superseded set"]
    superseded["query_contextualizer"].add(old_hash)
    assert guard.check(base) == []
