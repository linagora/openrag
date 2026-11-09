"""Renamed env vars: the old name keeps loading, with a warning, until the new one is set."""

from __future__ import annotations

import logging

import pytest
from core.config import load_config

_MINIMAL_YAML = "retriever:\n  type: single\n"

RENAMED = [
    ("MARKER_NUM_INSTANCES", "MARKER_POOL_SIZE", "marker_num_instances"),
    ("MARKER_CHILD_WORKERS_PER_INSTANCE", "MARKER_MAX_PROCESSES", "marker_child_workers_per_instance"),
    ("PYMUPDF_CHILD_WORKERS_PER_INDEXER", "PYMUPDF_POOL_SIZE", "pymupdf_child_workers_per_indexer"),
    ("DOCLING_NUM_INSTANCES", "DOCLING_POOL_SIZE", "docling_num_instances"),
]


@pytest.fixture
def config_dir(monkeypatch, tmp_path):
    (tmp_path / "config.yaml").write_text(_MINIMAL_YAML, encoding="utf-8")
    # Empty counts as unset, and load_dotenv() never overrides an existing
    # var: this shields the tests from any local .env.
    for new, old, _ in RENAMED:
        monkeypatch.setenv(new, "")
        monkeypatch.setenv(old, "")
    return tmp_path


def _warnings(caplog) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]


@pytest.mark.parametrize(("new", "old", "field"), RENAMED)
def test_the_new_name_sets_it(monkeypatch, config_dir, caplog, new, old, field):
    monkeypatch.setenv(new, "3")

    with caplog.at_level(logging.WARNING, logger="core.config.loader"):
        assert getattr(load_config(config_path=config_dir).loader, field) == 3
    assert _warnings(caplog) == []


@pytest.mark.parametrize(("new", "old", "field"), RENAMED)
def test_the_old_name_still_sets_it_with_a_warning(monkeypatch, config_dir, caplog, new, old, field):
    monkeypatch.setenv(old, "5")

    with caplog.at_level(logging.WARNING, logger="core.config.loader"):
        assert getattr(load_config(config_path=config_dir).loader, field) == 5
    assert _warnings(caplog) == [f"{old} was renamed {new}: rename it in your configuration"]


@pytest.mark.parametrize(("new", "old", "field"), RENAMED)
def test_the_new_name_wins_over_the_old_one(monkeypatch, config_dir, caplog, new, old, field):
    monkeypatch.setenv(new, "3")
    monkeypatch.setenv(old, "5")

    with caplog.at_level(logging.WARNING, logger="core.config.loader"):
        assert getattr(load_config(config_path=config_dir).loader, field) == 3
    assert _warnings(caplog) == [f"{old} is ignored: it was renamed {new}, which is set too. Remove it."]


@pytest.mark.parametrize(("new", "old", "field"), RENAMED)
def test_a_bad_old_value_is_reported_under_the_old_name(monkeypatch, config_dir, new, old, field):
    monkeypatch.setenv(old, "many")

    with pytest.raises(ValueError, match=f"Invalid value for {old}:"):
        load_config(config_path=config_dir)
