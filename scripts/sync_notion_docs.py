"""Sync the three refacto docs from Notion into `.claude/skills/openrag-refacto/`.

Reads config from environment variables (set by the GitHub workflow):

  NOTION_TOKEN               — integration token (required; read by notion2md)
  NOTION_PAGE_STRATEGY       — page ID for REFACTORING_STRATEGY_v1.md
  NOTION_PAGE_WORKFLOW       — page ID for REFACTORING_DEV_WORKFLOW.md
  NOTION_PAGE_GUIDE          — page ID for "Refactoring OpenRAG for Enterprise.md"

For each configured page, fetches the block tree, converts to markdown via
notion2md, and writes to the target file only if the content changed.
Per-page errors (e.g. 404 when the integration isn't shared on a page) are
caught so one bad page doesn't abort the rest.

Exit codes:
  0 — all configured pages synced (or skipped because their ID env var was unset)
  1 — at least one configured page failed to fetch
  2 — required config (NOTION_TOKEN) is missing
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from notion2md.exporter.block import StringExporter

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = REPO_ROOT / ".claude" / "skills" / "openrag-refacto"

# (env var holding the Notion page ID, output filename)
PAGES = [
    ("NOTION_PAGE_STRATEGY", "REFACTORING_STRATEGY_v1.md"),
    ("NOTION_PAGE_WORKFLOW", "REFACTORING_DEV_WORKFLOW.md"),
    ("NOTION_PAGE_GUIDE", "Refactoring OpenRAG for Enterprise.md"),
]


def fetch_markdown(page_id: str) -> str:
    return StringExporter(block_id=page_id).export()


def sync_page(page_id: str, output_path: Path) -> bool:
    """Write `output_path` if the fetched markdown differs. Return True on change."""
    new_content = fetch_markdown(page_id)
    if output_path.exists() and output_path.read_text(encoding="utf-8") == new_content:
        return False
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(new_content, encoding="utf-8")
    return True


def main() -> int:
    if not os.environ.get("NOTION_TOKEN"):
        print("error: NOTION_TOKEN not set", file=sys.stderr)
        return 2

    changed: list[str] = []
    skipped: list[str] = []
    failed: list[str] = []

    for env_var, filename in PAGES:
        page_id = os.environ.get(env_var, "").strip()
        if not page_id:
            print(f"skip: {env_var} not set")
            skipped.append(filename)
            continue
        target = OUTPUT_DIR / filename
        print(f"sync: {env_var} -> {target.relative_to(REPO_ROOT)}")
        try:
            if sync_page(page_id, target):
                changed.append(filename)
        except Exception as exc:
            print(f"fail: {filename}: {exc!r}", file=sys.stderr)
            failed.append(filename)

    if skipped:
        print(f"\nskipped ({len(skipped)}): {', '.join(skipped)}")
    if failed:
        print(f"failed ({len(failed)}): {', '.join(failed)}")
    if changed:
        print(f"changed ({len(changed)}): {', '.join(changed)}")
    else:
        print("no changes")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
