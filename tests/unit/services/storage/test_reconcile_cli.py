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
    spec = importlib.util.spec_from_file_location("reconcile_catalog_cli", scripts / "reconcile_catalog.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("findings,code", [(0, 0), (2, 1)])
def test_cli_streams_json_and_returns_drift_status(cli, monkeypatch, capsys, findings, code):
    async def scan(args):
        assert args.partition == "a"
        assert args.repair is False
        yield {"type": "summary", "orphan_chunks": findings}

    monkeypatch.setattr(cli, "scan", scan)
    assert cli.main(["--partition", "a"]) == code
    assert json.loads(capsys.readouterr().out) == {"type": "summary", "orphan_chunks": findings}


def test_cli_failure_emits_error_without_success_summary(cli, monkeypatch, capsys):
    async def scan(args):
        yield {"type": "orphan_chunks", "chunks": []}
        raise RuntimeError("storage unavailable")

    monkeypatch.setattr(cli, "scan", scan)
    assert cli.main(["--partition", "a", "--repair"]) == 2
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[-1]["type"] == "error"
    assert not any(e["type"] == "summary" for e in events)


@pytest.mark.parametrize(
    "args",
    [[], ["--partition", ""], ["--partition", "a", "--page-size", "0"], ["--partition", "a", "--grace-seconds", "nan"]],
)
def test_cli_rejects_invalid_arguments_before_connecting(cli, args):
    with pytest.raises(SystemExit) as error:
        cli.main(args)
    assert error.value.code == 2


async def test_scan_opens_existing_stores_without_migrations_and_closes_on_failure(cli, monkeypatch):
    import core.config
    import services.persistence.connection
    import services.storage.milvus_store
    import services.storage.reconciliation
    from core.config.infrastructure import RDBConfig, VectorDBConfig

    settings = SimpleNamespace(rdb=RDBConfig(database=None), vectordb=VectorDBConfig(collection_name="test"))
    monkeypatch.setattr(core.config, "load_config", lambda: settings)
    manager = AsyncMock()
    connection = MagicMock(return_value=manager)
    monkeypatch.setattr(services.persistence.connection, "ConnectionManager", connection)
    vectors = AsyncMock()
    monkeypatch.setattr(services.storage.milvus_store, "MilvusVectorStore", lambda config: vectors)

    async def failed_scan(*args, **kwargs):
        assert kwargs["repair"] is False
        yield {"type": "finding"}
        raise RuntimeError("scan interrupted")

    monkeypatch.setattr(services.storage.reconciliation, "reconcile_partition", failed_scan)
    args = SimpleNamespace(partition="a", repair=False, grace_seconds=3600, page_size=500)
    with pytest.raises(RuntimeError, match="scan interrupted"):
        _ = [e async for e in cli.scan(args)]
    config = connection.call_args.args[0]
    assert config.database == "partitions_for_collection_test"
    assert config.auto_create_database is False
    assert settings.rdb.database is None
    manager.initialize.assert_awaited_once()
    manager.run_migrations.assert_not_awaited()
    manager.shutdown.assert_awaited_once()
    vectors.aclose.assert_awaited_once()
