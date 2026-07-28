"""Tests for EvaluationConfig — env plumbing and the bounds on every field.

The env mapping is a hand-maintained table in ``core/config/loader.py`` and the
field names do not match the variable names (``EVAL_TASK_TIMEOUT`` sets
``task_timeout_seconds``), so a typo there is invisible until a deployment
retunes a limit and nothing happens.
"""

from __future__ import annotations

import pytest
from core.config import load_config
from core.config.evaluation import EvaluationConfig
from pydantic import ValidationError

# (env var, config attribute, value to set, expected parsed value)
_ENV_OVERRIDES = [
    ("PROMPTFOO_BIN", "promptfoo_bin", "/opt/promptfoo/bin/promptfoo", "/opt/promptfoo/bin/promptfoo"),
    ("EVAL_MAX_CORPUS_MB", "max_corpus_mb", "64", 64),
    ("EVAL_MAX_TESTSET_MB", "max_testset_mb", "2", 2),
    ("EVAL_MAX_TESTSET_ROWS", "max_testset_rows", "50", 50),
    ("EVAL_TOP_K", "top_k", "10", 10),
    ("EVAL_TASK_TIMEOUT", "task_timeout_seconds", "60", 60.0),
    ("EVAL_TASK_POLL_INTERVAL", "task_poll_seconds", "0.25", 0.25),
    ("EVAL_HTTP_TIMEOUT", "http_timeout_seconds", "30", 30.0),
    ("EVAL_PROMPTFOO_TIMEOUT", "promptfoo_timeout_seconds", "120", 120.0),
]


@pytest.mark.parametrize(("env_var", "attribute", "raw", "expected"), _ENV_OVERRIDES)
def test_every_eval_setting_is_reachable_from_the_environment(monkeypatch, tmp_path, env_var, attribute, raw, expected):
    (tmp_path / "config.yaml").write_text("retriever:\n  type: single\n", encoding="utf-8")
    monkeypatch.setenv(env_var, raw)

    settings = load_config(config_path=tmp_path)

    assert getattr(settings.evaluation, attribute) == expected


def test_the_env_table_covers_every_field():
    """A field added without an ``EVAL_*`` entry is a setting no deployment can
    actually reach — the whole reason these limits are config."""
    mapped = {attribute for _, attribute, _, _ in _ENV_OVERRIDES}

    assert mapped == set(EvaluationConfig.model_fields)


@pytest.mark.parametrize(
    "field",
    [
        "max_corpus_mb",
        "max_testset_mb",
        "max_testset_rows",
        "top_k",
        "task_timeout_seconds",
        "task_poll_seconds",
        "http_timeout_seconds",
        "promptfoo_timeout_seconds",
    ],
)
def test_non_positive_limits_are_rejected(field):
    """A zero or negative limit does not degrade gracefully: it reaches the
    runner as a cap that rejects every upload, or a timeout that has already
    expired. Fail at config load, where the error names the field."""
    with pytest.raises(ValidationError):
        EvaluationConfig(**{field: 0})

    with pytest.raises(ValidationError):
        EvaluationConfig(**{field: -1})


def test_an_empty_promptfoo_bin_is_rejected():
    with pytest.raises(ValidationError):
        EvaluationConfig(promptfoo_bin="")


def test_top_k_is_bounded_like_the_retrieval_pipeline():
    with pytest.raises(ValidationError):
        EvaluationConfig(top_k=1001)


def test_byte_properties_convert_from_megabytes():
    settings = EvaluationConfig(max_corpus_mb=3, max_testset_mb=2)

    assert settings.max_corpus_bytes == 3 * 1024 * 1024
    assert settings.max_testset_bytes == 2 * 1024 * 1024
