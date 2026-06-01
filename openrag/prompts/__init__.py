"""Bundled default prompt templates.

Plain-text prompt templates live in ``templates/`` and are read into the
``DEFAULT_SEEDS`` mapping at import time, keyed by filename stem (e.g.
``"sys_prompt_tmpl"`` for ``templates/sys_prompt_tmpl.txt``).

These are the first-boot defaults. At runtime the application resolves prompts
DB-first — per-partition overrides stored in Postgres take precedence — and
falls back to ``DEFAULT_SEEDS`` when no override exists.
"""

from __future__ import annotations

from pathlib import Path

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


def _load_seeds() -> dict[str, str]:
    """Read every ``templates/*.txt`` file into a stem -> contents mapping."""
    return {path.stem: path.read_text(encoding="utf-8") for path in sorted(TEMPLATES_DIR.glob("*.txt"))}


DEFAULT_SEEDS: dict[str, str] = _load_seeds()

__all__ = ["DEFAULT_SEEDS", "TEMPLATES_DIR"]
