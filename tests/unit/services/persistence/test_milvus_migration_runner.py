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
    with pytest.raises(SystemExit) as exc:
        _run(runner, monkeypatch, "--downgrade", "--dry-run")

    assert exc.value.code == 2
    assert runner.calls == []


def test_a_downgrade_goes_to_the_target_it_is_given(runner, monkeypatch):
    _run(runner, monkeypatch, "--downgrade", "--target", "2")

    assert runner.calls == [("downgrade", 2)]


def test_an_upgrade_still_defaults_to_the_latest_version(runner, monkeypatch):
    latest = runner._discover_migrations()[-1][0]

    _run(runner, monkeypatch)

    assert runner.calls == [("upgrade", latest)]


# ---------------------------------------------------------------------------
# Each migration script's own --downgrade, run standalone
# ---------------------------------------------------------------------------

_SCRIPTS = {
    1: "1.add_created_at_temporal_fields.py",
    2: "2.rebuild_text_analyzer.py",
    3: "3.split_vector_per_embedder.py",
}


def _load_script(monkeypatch, filename: str, stored_version: int):
    spec = importlib.util.spec_from_file_location(f"milvus_script_{filename[0]}", _RUNNER_PATH.parent / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    calls: list[str] = []
    module.calls = calls

    class FakeClient:
        def __init__(self, uri: str) -> None:
            pass

        def has_collection(self, name: str) -> bool:
            return True

        def describe_collection(self, name: str) -> dict:
            return {"properties": {module.SCHEMA_VERSION_PROPERTY_KEY: str(stored_version)}}

    monkeypatch.setattr(module, "MilvusClient", FakeClient)
    monkeypatch.setattr(
        module,
        "load_config",
        lambda: SimpleNamespace(vectordb=SimpleNamespace(host="milvus", port=19530, collection_name="vdb_test")),
    )
    monkeypatch.setattr(module, "downgrade", lambda client, name, dry_run=False: calls.append("downgrade"))
    monkeypatch.setattr(module, "upgrade", lambda client, name, dry_run=False: calls.append("upgrade"))
    return module


@pytest.mark.parametrize("version", sorted(_SCRIPTS))
@pytest.mark.parametrize("extra_args", [[], ["--dry-run"]])
def test_a_standalone_downgrade_of_another_version_is_refused(monkeypatch, version, extra_args):
    """Run on its own, a script's --downgrade reverts its step whatever the
    collection's version: version 2's, on a version 3 collection, swaps the
    pre-upgrade backup back in. It now runs only on its own version."""
    script = _load_script(monkeypatch, _SCRIPTS[version], stored_version=version + 1)
    monkeypatch.setattr("sys.argv", [_SCRIPTS[version], "--downgrade", *extra_args])

    with pytest.raises(SystemExit) as exc:
        script.main()

    assert exc.value.code == 2
    assert script.calls == []


@pytest.mark.parametrize("version", sorted(_SCRIPTS))
def test_a_standalone_downgrade_of_its_own_version_runs(monkeypatch, version):
    script = _load_script(monkeypatch, _SCRIPTS[version], stored_version=version)
    monkeypatch.setattr("sys.argv", [_SCRIPTS[version], "--downgrade"])

    script.main()

    assert script.calls == ["downgrade"]


@pytest.mark.parametrize("version", sorted(_SCRIPTS))
def test_a_standalone_upgrade_is_not_gated_on_the_stored_version(monkeypatch, version):
    script = _load_script(monkeypatch, _SCRIPTS[version], stored_version=version - 1)
    monkeypatch.setattr("sys.argv", [_SCRIPTS[version]])

    script.main()

    assert script.calls == ["upgrade"]
