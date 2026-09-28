"""The Milvus migration runner's command line (``migrate.py``).

The runner is a script loaded by path, like the migrations it runs, so it is
imported the same way. Milvus and the configuration are replaced by fakes that
record what the runner asked of them.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

_RUNNER_PATH = (
    Path(__file__).resolve().parents[4]
    / "openrag"
    / "services"
    / "persistence"
    / "migrations"
    / "milvus"
    / "migrate.py"
)


@pytest.fixture
def runner(monkeypatch):
    spec = importlib.util.spec_from_file_location("milvus_migration_runner", _RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    calls: list[tuple[str, int]] = []
    module.calls = calls
    connected: list[str] = []
    module.connected = connected

    class FakeClient:
        def __init__(self, uri: str) -> None:
            connected.append(uri)

        def has_collection(self, name: str) -> bool:
            return True

    monkeypatch.setattr(module, "MilvusClient", FakeClient)
    monkeypatch.setattr(
        module,
        "load_config",
        lambda: SimpleNamespace(vectordb=SimpleNamespace(host="milvus", port=19530, collection_name="vdb_test")),
    )
    monkeypatch.setattr(
        module, "run_upgrade", lambda client, name, migrations, target, dry_run: calls.append(("upgrade", target))
    )
    monkeypatch.setattr(
        module, "run_downgrade", lambda client, name, migrations, target, dry_run: calls.append(("downgrade", target))
    )
    return module


def _run(runner, monkeypatch, *args: str) -> None:
    monkeypatch.setattr("sys.argv", ["migrate.py", *args])
    runner.main()


def test_a_downgrade_without_a_target_is_refused_before_touching_milvus(runner, monkeypatch, capsys):
    """Without a target, a downgrade used to go down to version 0: reverting
    every migration, including version 2, whose rollback swaps the pre-upgrade
    backup collection back in. A rollback from 2.3.0 is exactly when an
    operator reaches for --downgrade, so a bare one must do nothing."""
    with pytest.raises(SystemExit) as exc:
        _run(runner, monkeypatch, "--downgrade")

    assert exc.value.code == 2
    assert "--downgrade needs --target" in capsys.readouterr().err
    assert runner.calls == []
    assert runner.connected == []


def test_a_dry_run_downgrade_without_a_target_is_refused_too(runner, monkeypatch):
    with pytest.raises(SystemExit):
        _run(runner, monkeypatch, "--downgrade", "--dry-run")

    assert runner.calls == []


def test_a_downgrade_goes_to_the_target_it_is_given(runner, monkeypatch):
    _run(runner, monkeypatch, "--downgrade", "--target", "2")

    assert runner.calls == [("downgrade", 2)]


def test_an_upgrade_still_defaults_to_the_latest_version(runner, monkeypatch):
    latest = runner._discover_migrations()[-1][0]

    _run(runner, monkeypatch)

    assert runner.calls == [("upgrade", latest)]
