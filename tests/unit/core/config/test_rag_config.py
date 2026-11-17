"""The ``rag`` config: the default ``rag_mode``, query rewriting, and the deprecated ``RAG_MODE``."""

from __future__ import annotations

import pytest
from core.config import load_config
from core.config.retrieval import RAGConfig, _warn_deprecated_mode
from pydantic import ValidationError


@pytest.fixture
def conf(tmp_path):
    (tmp_path / "config.yaml").write_text("retriever:\n  type: single\n", encoding="utf-8")
    return tmp_path


@pytest.fixture(autouse=True)
def _warn_again():
    _warn_deprecated_mode.cache_clear()
    yield
    _warn_deprecated_mode.cache_clear()


def test_by_default_a_request_runs_classic_with_its_question_rewritten(conf):
    settings = load_config(config_path=conf)

    assert (settings.rag.default_mode, settings.rag.query_rewrite) == ("classic", True)


def test_the_shipped_config_runs_classic_with_its_question_rewritten(monkeypatch):
    for name in ("RAG_DEFAULT_MODE", "RAG_QUERY_REWRITE", "RAG_MODE"):
        monkeypatch.delenv(name, raising=False)

    rag = load_config().rag

    assert (rag.default_mode, rag.query_rewrite) == ("classic", True)


def test_the_default_mode_and_query_rewriting_are_set_from_env(monkeypatch, conf):
    monkeypatch.setenv("RAG_DEFAULT_MODE", "agentic")
    monkeypatch.setenv("RAG_QUERY_REWRITE", "false")

    settings = load_config(config_path=conf)

    assert (settings.rag.default_mode, settings.rag.query_rewrite) == ("agentic", False)


def test_a_default_mode_that_is_not_a_mode_fails_the_config(monkeypatch, conf):
    monkeypatch.setenv("RAG_DEFAULT_MODE", "SimpleRag")

    with pytest.raises(ValidationError, match="default_mode"):
        load_config(config_path=conf)


@pytest.mark.parametrize(("mode", "rewrite"), [("SimpleRag", False), ("ChatBotRag", True)])
def test_the_deprecated_rag_mode_still_decides_and_says_what_replaces_it(monkeypatch, conf, caplog, mode, rewrite):
    monkeypatch.setenv("RAG_MODE", mode)
    monkeypatch.setenv("RAG_QUERY_REWRITE", str(not rewrite).lower())

    with caplog.at_level("WARNING"):
        settings = load_config(config_path=conf)

    assert settings.rag.query_rewrite is rewrite, "a deployment that set RAG_MODE keeps what it chose"
    assert f"RAG_QUERY_REWRITE={str(rewrite).lower()}" in caplog.text
    assert "deprecated" in caplog.text


def test_the_deprecated_rag_mode_is_read_from_a_config_file_too(tmp_path):
    (tmp_path / "config.yaml").write_text("rag:\n  mode: SimpleRag\n", encoding="utf-8")

    assert load_config(config_path=tmp_path).rag.query_rewrite is False


def test_an_unknown_rag_mode_fails_the_config_rather_than_every_request(monkeypatch, conf):
    monkeypatch.setenv("RAG_MODE", "FancyRag")

    with pytest.raises(ValidationError, match="RAG_QUERY_REWRITE"):
        load_config(config_path=conf)


def test_the_deprecated_mode_is_not_kept_on_the_config():
    config = RAGConfig(mode="SimpleRag")

    assert "mode" not in config.model_dump()
    assert config.query_rewrite is False
