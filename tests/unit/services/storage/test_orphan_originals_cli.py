import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def cli(monkeypatch):
    scripts = Path(__file__).resolve().parents[4] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("find_orphan_originals_cli", scripts / "find_orphan_originals.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("orphans,code", [(0, 0), (2, 1)])
def test_cli_streams_json_and_returns_whether_it_found_orphans(cli, monkeypatch, capsys, orphans, code):
    async def scan(args):
        assert args.min_age_hours == 24
        assert args.delete is False
        yield {"type": "summary", "orphans": orphans}

    monkeypatch.setattr(cli, "scan", scan)
    assert cli.main([]) == code
    assert json.loads(capsys.readouterr().out) == {"type": "summary", "orphans": orphans}


def test_cli_failure_emits_an_error_without_a_summary(cli, monkeypatch, capsys):
    async def scan(args):
        yield {"type": "orphan", "path": "/data/1_a.pdf"}
        raise RuntimeError("Nothing was deleted.")

    monkeypatch.setattr(cli, "scan", scan)
    assert cli.main(["--delete"]) == 2
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[-1] == {"type": "error", "message": "Nothing was deleted."}
    assert not any(e["type"] == "summary" for e in events)


@pytest.mark.parametrize("args", [["--min-age-hours", "0"], ["--min-age-hours", "0.5"], ["--min-age-hours", "nan"]])
def test_cli_rejects_a_short_minimum_age_before_connecting(cli, args, monkeypatch):
    scan = MagicMock(side_effect=AssertionError("Invalid arguments must not open storage"))
    monkeypatch.setattr(cli, "scan", scan)
    with pytest.raises(SystemExit) as error:
        cli.main(args)
    assert error.value.code == 2
    scan.assert_not_called()


async def test_scan_opens_the_catalog_without_migrations_and_closes_it_on_failure(cli, monkeypatch, tmp_path):
    import core.config
    import services.persistence.connection
    import services.storage.orphan_originals
    from core.config.infrastructure import RDBConfig, VectorDBConfig

    settings = SimpleNamespace(
        rdb=RDBConfig(database=None),
        vectordb=VectorDBConfig(collection_name="test"),
        paths=SimpleNamespace(data_dir=tmp_path),
    )
    monkeypatch.setattr(core.config, "load_config", lambda: settings)
    manager = AsyncMock()
    connection = MagicMock(return_value=manager)
    monkeypatch.setattr(services.persistence.connection, "ConnectionManager", connection)

    async def failed_scan(repo, data_dir, **kwargs):
        assert data_dir == tmp_path
        assert kwargs == {"min_age_seconds": 48 * 3600, "delete": True}
        yield {"type": "orphan"}
        raise RuntimeError("scan interrupted")

    monkeypatch.setattr(services.storage.orphan_originals, "scan_orphan_originals", failed_scan)
    args = SimpleNamespace(min_age_hours=48, delete=True)
    with pytest.raises(RuntimeError, match="scan interrupted"):
        _ = [e async for e in cli.scan(args)]
    config = connection.call_args.args[0]
    assert config.database == "partitions_for_collection_test"
    assert config.auto_create_database is False
    manager.initialize.assert_awaited_once()
    manager.run_migrations.assert_not_awaited()
    manager.shutdown.assert_awaited_once()
