"""The revision graph has exactly one head.

Two heads make `alembic upgrade head` refuse — "Multiple head revisions are
present" — which takes down every startup and every integration suite that
migrates a database. It is the normal outcome of merging a long-lived branch
that added a revision, and nothing else here notices: each revision's own test
passes, and so does a graph with two of them.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

ALEMBIC_DIR = Path(__file__).resolve().parents[4] / "openrag" / "services" / "persistence" / "migrations" / "alembic"


@pytest.fixture
def script_directory(monkeypatch) -> ScriptDirectory:
    # The revision modules import `schema_helpers` from the alembic directory
    # itself, the way alembic runs them.
    monkeypatch.syspath_prepend(str(ALEMBIC_DIR))
    config = Config(str(ALEMBIC_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(ALEMBIC_DIR))
    return ScriptDirectory.from_config(config)


def test_the_revision_graph_has_a_single_head(script_directory):
    heads = script_directory.get_heads()

    assert len(heads) == 1, (
        f"{len(heads)} alembic heads: {sorted(heads)}. `upgrade head` cannot choose between them. "
        "Add a merge revision whose down_revision is the tuple of both heads."
    )


def test_every_revision_is_reachable_from_the_head(script_directory):
    """A revision no path reaches is never applied, however correct it is."""
    (head,) = script_directory.get_heads()
    reachable = {revision.revision for revision in script_directory.walk_revisions("base", head)}
    all_revisions = {revision.revision for revision in script_directory.walk_revisions()}

    assert all_revisions == reachable, f"unreachable from head: {sorted(all_revisions - reachable)}"
