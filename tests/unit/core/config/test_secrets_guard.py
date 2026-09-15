"""Tests for :mod:`core.config.secrets_guard`.

Two jobs. The first half pins the policy itself: a published value is refused,
a generated one is not, the opt-out is an opt-out, and the message never echoes
the value it is complaining about.

The second half is a drift gate. The same policy is expressed twice — once in
Python for runtime, once in the chart for ``helm template`` — because neither
covers what the other does: the chart validates nothing under ``existingSecret``
or an external secret store, and the runtime guard cannot fail an install before
it happens. Two copies drift, and this list has drifted before, so the build
fails when they disagree.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from core.config.secrets_guard import (
    ALLOW_INSECURE_SECRETS_ENV_VAR,
    KNOWN_DEFAULT_SECRETS,
    MIN_SECRET_LENGTH,
    SECRET_SPECS,
    enforce_secret_policy,
    find_insecure_secrets,
    is_known_default,
)
from core.utils.exceptions import ConfigError

_REPO_ROOT = Path(__file__).resolve().parents[4]
_CHART_SECRETS_TEMPLATE = _REPO_ROOT / "infra/charts/openrag-stack/templates/secrets-env.yaml"

#: Long enough to clear the floor, and not a value this project publishes.
_GENERATED = "b8f3d1a97c4e2065b8f3d1a97c4e2065"


# ---------------------------------------------------------------------------
# The policy
# ---------------------------------------------------------------------------


def test_generated_values_pass():
    env = {spec.env_var: _GENERATED for spec in SECRET_SPECS}
    assert find_insecure_secrets(env=env) == []


def test_absent_values_pass():
    """An unset credential is not a policy failure.

    ``AUTH_TOKEN`` unset is a supported development mode, and an unconfigured
    optional integration is normal. Requiredness is enforced where an install
    happens — the chart and compose — not here.
    """
    assert find_insecure_secrets(env={}) == []


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_values_pass(blank):
    """Set-but-empty reads the same as unset everywhere else in the loader, and
    whitespace-only is the same thing with a typo."""
    assert find_insecure_secrets(env={spec.env_var: blank for spec in SECRET_SPECS}) == []


@pytest.mark.parametrize(
    ("env_var", "value"),
    [
        ("AUTH_TOKEN", "or-openrag-1234"),
        ("POSTGRES_PASSWORD", "postgres"),
        ("POSTGRES_PASSWORD", "root_password"),
        ("CHAINLIT_AUTH_SECRET", "openrag-dev-secret"),
        ("MINIO_ACCESS_KEY", "minioadmin"),
        ("MINIO_SECRET_KEY", "minioadmin"),
        ("GRAFANA_ADMIN_PASSWORD", "replace-with-a-strong-password"),
        ("API_KEY", "sk-xxxx"),
        ("HF_TOKEN", "hf_xxxx"),
        ("AUTH_TOKEN", "test-admin-token"),
    ],
)
def test_published_defaults_are_refused(env_var, value):
    problems = find_insecure_secrets(env={env_var: value})
    assert len(problems) == 1
    assert env_var in problems[0]


def test_denylist_is_case_insensitive_and_trimmed():
    """A copy is a copy however it was pasted."""
    assert find_insecure_secrets(env={"AUTH_TOKEN": "  OR-OpenRAG-1234  "})


def test_short_value_refused_for_credentials_we_define():
    problems = find_insecure_secrets(env={"AUTH_TOKEN": "short"})
    assert len(problems) == 1
    assert str(MIN_SECRET_LENGTH) in problems[0]


def test_short_value_allowed_for_upstream_credentials():
    """An inference provider decides how long its own API key is."""
    assert find_insecure_secrets(env={"API_KEY": "abc"}) == []


def test_no_credential_sentinel_is_allowed_for_upstream_credentials():
    """``EMPTY`` says "this endpoint needs none" — the shipped value for the
    bundled vLLM, embedder and reranker, not a weak credential."""
    assert "empty" not in KNOWN_DEFAULT_SECRETS
    env = {spec.env_var: "EMPTY" for spec in SECRET_SPECS if not spec.enforce_min_length}
    assert find_insecure_secrets(env=env) == []


@pytest.mark.parametrize("env_var", ["AUTH_TOKEN", "MINIO_SECRET_KEY"])
def test_no_credential_sentinel_is_refused_where_we_define_the_format(env_var):
    """``EMPTY`` is a five-character credential. It is meaningful only for an
    endpoint that accepts no credential; as an API bearer token or an
    object-store key it is just a weak one, and the length floor says so."""
    assert find_insecure_secrets(env={env_var: "EMPTY"})


def test_every_reported_problem_names_one_variable():
    env = {"AUTH_TOKEN": "or-openrag-1234", "POSTGRES_PASSWORD": "postgres"}
    problems = find_insecure_secrets(env=env)
    assert len(problems) == 2


# ---------------------------------------------------------------------------
# Enforcement
# ---------------------------------------------------------------------------


def test_enforce_raises_config_error():
    with pytest.raises(ConfigError) as exc_info:
        enforce_secret_policy(env={"AUTH_TOKEN": "or-openrag-1234"})
    assert exc_info.value.code == "INSECURE_SECRET_CONFIGURATION"


def test_enforce_never_echoes_the_value():
    """The message reaches logs and a failed container's status output."""
    with pytest.raises(ConfigError) as exc_info:
        enforce_secret_policy(env={"AUTH_TOKEN": "or-openrag-1234"})
    assert "or-openrag-1234" not in str(exc_info.value)


def test_enforce_passes_on_generated_values():
    enforce_secret_policy(env={"AUTH_TOKEN": _GENERATED})


def test_opt_out_downgrades_to_a_warning(caplog):
    env = {"AUTH_TOKEN": "or-openrag-1234", ALLOW_INSECURE_SECRETS_ENV_VAR: "true"}
    with caplog.at_level("WARNING"):
        enforce_secret_policy(env=env)
    assert ALLOW_INSECURE_SECRETS_ENV_VAR in caplog.text


def test_opt_out_is_never_an_opt_in():
    """Anything other than an explicit ``true`` leaves the guard enforcing."""
    for value in ("false", "0", "", "yes", "TRUE ", "1"):
        env = {"AUTH_TOKEN": "or-openrag-1234", ALLOW_INSECURE_SECRETS_ENV_VAR: value}
        if value.strip().lower() == "true":
            enforce_secret_policy(env=env)
            continue
        with pytest.raises(ConfigError):
            enforce_secret_policy(env=env)


def test_environment_wins_over_settings():
    """The environment is the layer an operator sets, and the layer
    ``load_dotenv`` has already merged by the time the guard runs."""

    class _Rdb:
        password = _GENERATED

    class _Settings:
        rdb = _Rdb()

    assert find_insecure_secrets(_Settings(), env={"POSTGRES_PASSWORD": "postgres"})


def test_settings_are_consulted_when_the_environment_is_silent():
    """``rdb.password`` can also be written into ``conf/config.yaml``."""

    class _Rdb:
        password = "postgres"

    class _Settings:
        rdb = _Rdb()

    assert find_insecure_secrets(_Settings(), env={})


# ---------------------------------------------------------------------------
# Drift gate: the chart must say the same thing
# ---------------------------------------------------------------------------


def _chart_list(name: str) -> list[str]:
    """Pull one ``{{- $name := list "a" "b" }}`` out of the chart template."""
    text = _CHART_SECRETS_TEMPLATE.read_text()
    pattern = r"\$" + re.escape(name) + r"\s*:=\s*list\b(.*?)\}\}"
    match = re.search(pattern, text, re.DOTALL)
    assert match, f"${name} not found in {_CHART_SECRETS_TEMPLATE}"
    return re.findall(r'"([^"]*)"', match.group(1))


def test_chart_denylist_matches_python():
    chart_values = {value.strip().casefold() for value in _chart_list("placeholderSecrets")}
    assert chart_values == set(KNOWN_DEFAULT_SECRETS), (
        "The chart's placeholder denylist and KNOWN_DEFAULT_SECRETS have drifted. "
        "Both enforce the same policy at different times; edit both or neither."
    )


def test_chart_length_floor_matches_python():
    text = _CHART_SECRETS_TEMPLATE.read_text()
    match = re.search(r"\$minSecretLength\s*:=\s*(\d+)", text)
    assert match, "$minSecretLength not found in the chart template"
    assert int(match.group(1)) == MIN_SECRET_LENGTH


def test_chart_length_checked_keys_match_python():
    chart_keys = set(_chart_list("lengthCheckedSecrets"))
    python_keys = {spec.env_var for spec in SECRET_SPECS if spec.enforce_min_length}
    assert chart_keys == python_keys


def test_chart_required_keys_are_known_to_the_guard():
    """The chart may require more than the guard checks, but never a name the
    guard has never heard of — that would mean a secret nobody documented."""
    known = {spec.env_var for spec in SECRET_SPECS}
    assert set(_chart_list("requiredSecrets")) <= known


# ---------------------------------------------------------------------------
# Drift gate: the example env files must not ship a live credential
# ---------------------------------------------------------------------------

_EXAMPLE_ENV_FILES = (
    "infra/compose/.env.example",
    "docs/assets/env_example.env",
    "docs/assets/env_linux_gpu.env",
)

#: Names whose example value is a credential rather than a setting. A hostname
#: or a model name in an example file is fine; these are not.
_CREDENTIAL_KEYS = frozenset(spec.env_var for spec in SECRET_SPECS)


def _assignments(text: str) -> list[tuple[str, str]]:
    """Every ``KEY=value`` pair, including ones behind a single comment marker.

    A commented example is still a value someone will uncomment and use, and
    every occurrence counts: keeping only the first would let a later usable
    credential slip past this gate.
    """
    pairs: list[tuple[str, str]] = []
    for line in text.splitlines():
        stripped = line.lstrip("# ").strip()
        match = re.match(r"^([A-Z0-9_]+)=(.*)$", stripped)
        if match:
            pairs.append((match.group(1), match.group(2).strip()))
    return pairs


@pytest.mark.parametrize("relative_path", _EXAMPLE_ENV_FILES)
def test_example_env_files_ship_no_usable_credential(relative_path):
    """These files exist to be copied, so a working value in one is a
    credential installed by everybody who copies it. There are three of them
    and they have drifted before, which is why all three are checked here."""
    path = _REPO_ROOT / relative_path
    assert path.exists(), f"{relative_path} has moved; update this test"

    offenders = [
        key
        for key, value in _assignments(path.read_text())
        # "EMPTY" is the documented no-credential sentinel, not a usable one.
        if key in _CREDENTIAL_KEYS and value and value != "EMPTY" and not is_known_default(value)
    ]
    assert not offenders, (
        f"{relative_path} ships a usable value for {offenders}. Example files must carry "
        f"the generator placeholder so a copy fails closed instead of installing a credential."
    )


# ---------------------------------------------------------------------------
# The wiring: the policy is only worth anything if something calls it
# ---------------------------------------------------------------------------


def test_get_settings_enforces_the_policy(monkeypatch):
    """Without this, the enforcement call can be deleted from ``get_settings``
    and every other test in the suite still passes — which is exactly how a
    boot guard quietly stops guarding.

    The suite sets ``ALLOW_INSECURE_SECRETS`` globally (see pyproject.toml) so
    a developer's own ``.env`` cannot decide whether it passes; this test opts
    back in. ``load_dotenv`` does not override variables already set, so the
    value monkeypatched here wins over any ``.env`` on disk.
    """
    from core.config import get_settings

    monkeypatch.delenv(ALLOW_INSECURE_SECRETS_ENV_VAR, raising=False)
    monkeypatch.setenv("AUTH_TOKEN", "or-openrag-1234")
    get_settings.cache_clear()
    try:
        with pytest.raises(ConfigError) as exc_info:
            get_settings()
        assert exc_info.value.code == "INSECURE_SECRET_CONFIGURATION"
    finally:
        get_settings.cache_clear()


def test_get_settings_returns_settings_when_the_policy_passes(monkeypatch):
    from core.config import get_settings

    monkeypatch.delenv(ALLOW_INSECURE_SECRETS_ENV_VAR, raising=False)
    monkeypatch.setenv("AUTH_TOKEN", _GENERATED)
    get_settings.cache_clear()
    try:
        assert get_settings() is not None
    finally:
        get_settings.cache_clear()


def test_generator_placeholder_is_refused_by_the_guard():
    """``scripts/gen_env.py`` and the denylist name the marker independently.
    If they drift, ``cp .env.example .env`` starts succeeding again — the one
    failure this whole arrangement exists to produce."""
    source = (_REPO_ROOT / "scripts/gen_env.py").read_text()
    match = re.search(r'^PLACEHOLDER = "([^"]+)"', source, re.MULTILINE)
    assert match, "PLACEHOLDER not found in scripts/gen_env.py"
    assert is_known_default(match.group(1))
