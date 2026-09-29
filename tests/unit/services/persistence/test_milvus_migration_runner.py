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
    module.stored_version = "3"
    errors: list[str] = []
    module.errors = errors
    module.real_run_downgrade = module.run_downgrade

    class FakeClient:
        def __init__(self, uri: str) -> None:
            connected.append(uri)

        def has_collection(self, name: str) -> bool:
            return True

        def describe_collection(self, name: str) -> dict:
            if module.stored_version is None:
                return {"properties": {}}
            return {"properties": {module.SCHEMA_VERSION_PROPERTY_KEY: module.stored_version}}

    class FakeLogger:
        def info(self, message: str) -> None:
            pass

        def warning(self, message: str) -> None:
            pass

        def error(self, message: str) -> None:
            errors.append(message)

    monkeypatch.setattr(module, "MilvusClient", FakeClient)
    monkeypatch.setattr(module, "logger", FakeLogger())
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


@pytest.mark.parametrize("mode", [[], ["--downgrade"]], ids=["upgrade", "downgrade"])
@pytest.mark.parametrize("extra_args", [[], ["--dry-run"]])
@pytest.mark.parametrize("target", ["-1", "latest+1"])
def test_a_target_outside_the_migrations_is_refused_before_touching_milvus(
    runner, monkeypatch, capsys, mode, extra_args, target
):
    """--downgrade --target -1 selected every migration, as --target 0 does, so
    on a version 3 collection it reached version 2's backup swap."""
    latest = runner._discover_migrations()[-1][0]
    value = str(latest + 1) if target == "latest+1" else target

    with pytest.raises(SystemExit) as exc:
        _run(runner, monkeypatch, *mode, *extra_args, "--target", value)

    assert exc.value.code == 2
    assert f"--target must be between 0 and {latest}" in capsys.readouterr().err
    assert runner.calls == []
    assert runner.connected == []


@pytest.mark.parametrize("extra_args", [[], ["--dry-run"]])
def test_a_downgrade_above_the_collections_version_is_refused(runner, monkeypatch, extra_args):
    runner.stored_version = "2"

    with pytest.raises(SystemExit) as exc:
        _run(runner, monkeypatch, "--downgrade", *extra_args, "--target", "3")

    assert exc.value.code == 2
    assert runner.errors == ["Cannot downgrade to version 3: the collection is at version 2. Nothing was changed."]
    assert runner.calls == []


def test_a_downgrade_to_the_collections_own_version_is_allowed(runner, monkeypatch):
    """Control for the refusal above: the boundary itself is a no-op downgrade,
    left to the runner, which reports that there is nothing to do."""
    runner.stored_version = "2"

    _run(runner, monkeypatch, "--downgrade", "--target", "2")

    assert runner.calls == [("downgrade", 2)]


def test_a_downgrade_to_version_0_is_still_allowed(runner, monkeypatch):
    """0 is the version before any migration, a documented rollback target."""
    _run(runner, monkeypatch, "--downgrade", "--target", "0")

    assert runner.calls == [("downgrade", 0)]
    assert runner.errors == []


def test_an_upgrade_to_version_0_is_allowed_on_an_unstamped_collection(runner, monkeypatch):
    runner.stored_version = None

    _run(runner, monkeypatch, "--target", "0")

    assert runner.calls == [("upgrade", 0)]


def test_an_unstamped_collection_is_upgraded_from_version_0(runner, monkeypatch):
    latest = runner._discover_migrations()[-1][0]
    runner.stored_version = None

    _run(runner, monkeypatch)

    assert runner.calls == [("upgrade", latest)]


def test_an_upgrade_below_the_collections_version_is_refused(runner, monkeypatch):
    """The mirror of a downgrade above it: a missing --downgrade used to
    report "already up to date" and exit 0."""
    with pytest.raises(SystemExit) as exc:
        _run(runner, monkeypatch, "--target", "1")

    assert exc.value.code == 2
    assert runner.errors == [
        "Cannot upgrade to version 1: the collection is already at version 3. To go back, pass --downgrade. "
        "Nothing was changed."
    ]
    assert runner.calls == []


@pytest.mark.parametrize("stamp", ["abc", "-1"])
@pytest.mark.parametrize("mode", [["--target", "3"], ["--downgrade", "--target", "2"]], ids=["upgrade", "downgrade"])
def test_a_collection_with_an_unknown_version_is_refused(runner, monkeypatch, stamp, mode):
    """Read as 0, a corrupt stamp made an upgrade replay every migration."""
    runner.stored_version = stamp

    with pytest.raises(SystemExit) as exc:
        _run(runner, monkeypatch, *mode)

    assert exc.value.code == 2
    assert runner.errors == [f"'vdb_test' has schema version '{stamp}', which is not a version. Nothing was changed."]
    assert runner.calls == []


@pytest.mark.parametrize("mode", [[], ["--downgrade", "--target", "2"]], ids=["upgrade", "downgrade"])
def test_a_collection_newer_than_the_runner_is_refused(runner, monkeypatch, mode):
    """At a version this runner has no script for, a downgrade to 2 reverted
    version 3 under it, and version 3's downgrade then stamped it 2."""
    latest = runner._discover_migrations()[-1][0]
    runner.stored_version = str(latest + 1)

    with pytest.raises(SystemExit) as exc:
        _run(runner, monkeypatch, *mode)

    assert exc.value.code == 2
    assert f"newer than this runner's latest ({latest})" in runner.errors[0]
    assert runner.calls == []


def test_a_downgrade_reverts_the_steps_above_the_target_newest_first(runner, monkeypatch):
    """Through the real run_downgrade: the step selection the refusals above protect."""
    reverted: list[int] = []

    def load(path: Path) -> SimpleNamespace:
        version = int(path.name.split(".")[0])
        return SimpleNamespace(
            TARGET_VERSION=version,
            upgrade=lambda client, name, dry_run=False: None,
            downgrade=lambda client, name, dry_run=False: reverted.append(version),
        )

    monkeypatch.setattr(runner, "run_downgrade", runner.real_run_downgrade)
    monkeypatch.setattr(runner, "_load_module", load)

    _run(runner, monkeypatch, "--downgrade", "--target", "1")

    assert reverted == [3, 2]


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
@pytest.mark.parametrize("offset", [1, -1], ids=["newer", "older"])
@pytest.mark.parametrize("extra_args", [[], ["--dry-run"]])
def test_a_standalone_downgrade_of_another_version_is_refused(monkeypatch, version, offset, extra_args):
    """Run on its own, a script's --downgrade reverts its step whatever the
    collection's version: version 2's, on a version 3 collection, swaps the
    pre-upgrade backup back in. It now runs only on its own version."""
    script = _load_script(monkeypatch, _SCRIPTS[version], stored_version=version + offset)
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
