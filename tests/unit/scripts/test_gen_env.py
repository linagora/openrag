"""Regression tests for ``scripts/gen_env.py`` — the one command that turns
``.env.example`` into a runnable ``.env``.

Two of these cover failures that reported success, which is the dangerous shape
for a bootstrap script: an operator who follows the documented upgrade path and
sees exit 0 has no reason to look again at a credential that was never written.
"""

from __future__ import annotations

import importlib.util
import os
import stat
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SPEC = importlib.util.spec_from_file_location("gen_env", _REPO_ROOT / "scripts" / "gen_env.py")
gen_env = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(gen_env)


# ---------------------------------------------------------------------------
# The temporary file must not follow a link someone else planted
# ---------------------------------------------------------------------------


def test_write_private_ignores_a_planted_symlink(tmp_path: Path) -> None:
    """The temp path used to be ``.{name}.tmp`` — predictable — and was opened
    with ``O_CREAT`` but no ``O_EXCL``, so an existing symlink there was
    followed. The 0600 mode applies only when ``open`` creates the file, so the
    credentials landed in the link's target with its own permissions.
    """
    target = tmp_path / "attacker_readable"
    target.write_text("")
    target.chmod(0o644)
    output = tmp_path / ".env"
    (tmp_path / f".{output.name}.tmp").symlink_to(target)

    gen_env._write_private(output, "AUTH_TOKEN=or-secret\n")

    assert target.read_text() == "", "credentials were written through the planted symlink"
    assert not output.is_symlink()
    assert output.read_text() == "AUTH_TOKEN=or-secret\n"
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_write_private_replaces_a_symlinked_output_rather_than_writing_through_it(tmp_path: Path) -> None:
    """``os.replace`` swaps the name, so a link at the destination is replaced."""
    target = tmp_path / "elsewhere"
    target.write_text("")
    output = tmp_path / ".env"
    output.symlink_to(target)

    gen_env._write_private(output, "AUTH_TOKEN=or-secret\n")

    assert target.read_text() == ""
    assert not output.is_symlink()


def test_write_private_leaves_no_temporary_file_behind(tmp_path: Path) -> None:
    output = tmp_path / ".env"
    gen_env._write_private(output, "A=1\n")

    assert sorted(p.name for p in tmp_path.iterdir()) == [".env"]


# ---------------------------------------------------------------------------
# A variable the template gained must be added, not merely reported
# ---------------------------------------------------------------------------


def _write(path: Path, text: str) -> Path:
    path.write_text(text)
    return path


def test_rerun_adds_a_variable_the_template_gained(tmp_path: Path) -> None:
    """The documented upgrade path: add a secret to the example, re-run, get it
    generated. It used to warn on stderr and exit 0, leaving it unset."""
    template = _write(tmp_path / ".env.example", "EXISTING=keep-me\nAUTH_TOKEN=__GENERATE_ME__\n")
    output = _write(tmp_path / ".env", "EXISTING=operator-value\n")

    rc = gen_env.main(["-t", str(template), "-o", str(output)])

    assert rc == 0
    text = output.read_text()
    assert "AUTH_TOKEN=" in text, "the new template variable was not added"
    assert "__GENERATE_ME__" not in text, "the added variable kept its placeholder"
    assert "EXISTING=operator-value" in text, "an existing value was overwritten"


def test_rerun_does_not_regenerate_an_existing_credential(tmp_path: Path) -> None:
    """Regenerating a password the data was written under breaks the stack."""
    template = _write(tmp_path / ".env.example", "AUTH_TOKEN=__GENERATE_ME__\n")
    output = _write(tmp_path / ".env", "AUTH_TOKEN=or-alreadyset\n")

    gen_env.main(["-t", str(template), "-o", str(output)])

    assert "AUTH_TOKEN=or-alreadyset" in output.read_text()


# ---------------------------------------------------------------------------
# --check must validate template/output parity
# ---------------------------------------------------------------------------


def test_check_fails_when_a_template_variable_is_absent(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """An absent variable holds no placeholder, so scanning for markers alone
    reported success on a file missing a credential outright."""
    template = _write(tmp_path / ".env.example", "AUTH_TOKEN=__GENERATE_ME__\nOTHER=x\n")
    output = _write(tmp_path / ".env", "OTHER=x\n")

    rc = gen_env.main(["--check", "-t", str(template), "-o", str(output)])

    assert rc == 1
    assert "AUTH_TOKEN" in capsys.readouterr().err


def test_check_still_fails_on_a_leftover_placeholder(tmp_path: Path) -> None:
    template = _write(tmp_path / ".env.example", "AUTH_TOKEN=__GENERATE_ME__\n")
    output = _write(tmp_path / ".env", "AUTH_TOKEN=__GENERATE_ME__\n")

    assert gen_env.main(["--check", "-t", str(template), "-o", str(output)]) == 1


def test_check_passes_on_a_complete_file(tmp_path: Path) -> None:
    template = _write(tmp_path / ".env.example", "AUTH_TOKEN=__GENERATE_ME__\nOTHER=x\n")
    output = _write(tmp_path / ".env", "AUTH_TOKEN=or-generated\nOTHER=x\n")

    assert gen_env.main(["--check", "-t", str(template), "-o", str(output)]) == 0


def test_check_requires_nonempty_postgres_password_when_template_uses_it(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    template = _write(tmp_path / ".env.example", "POSTGRES_PASSWORD=__GENERATE_ME__\n")
    output = _write(tmp_path / ".env", "POSTGRES_PASSWORD=\n")

    assert gen_env.main(["--check", "-t", str(template), "-o", str(output)]) == 1
    assert "POSTGRES_PASSWORD must be non-empty" in capsys.readouterr().err


@pytest.mark.parametrize("key", ["MINIO_ACCESS_KEY", "MINIO_SECRET_KEY"])
def test_check_requires_nonempty_minio_credentials_when_template_uses_them(
    tmp_path: Path, capsys: pytest.CaptureFixture, key: str
) -> None:
    template = _write(tmp_path / ".env.example", f"{key}=__GENERATE_ME__\n")
    output = _write(tmp_path / ".env", f"{key}=\n")

    assert gen_env.main(["--check", "-t", str(template), "-o", str(output)]) == 1
    assert f"{key} must be non-empty" in capsys.readouterr().err


def test_check_compose_storage_accepts_effective_valid_credentials(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setenv("MINIO_ACCESS_KEY", "openrag123456")
    monkeypatch.setenv("MINIO_SECRET_KEY", "long-secret-value-123456")

    assert gen_env.main(["--check-compose-storage"]) == 0
    assert "pass policy" in capsys.readouterr().out


def test_check_compose_postgres_accepts_effective_valid_password(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setenv("POSTGRES_PASSWORD", "strong-password-value")

    assert gen_env.main(["--check-compose-postgres"]) == 0
    assert "pass policy" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("", "must be non-empty"),
        ("postgres", "does not satisfy secret policy"),
        ("short", "does not satisfy secret policy"),
    ],
)
def test_check_compose_postgres_rejects_invalid_password(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, value: str, message: str
) -> None:
    monkeypatch.delenv("ALLOW_INSECURE_SECRETS", raising=False)
    monkeypatch.setenv("POSTGRES_PASSWORD", value)

    assert gen_env.main(["--check-compose-postgres"]) == 1
    error = capsys.readouterr().err
    assert f"POSTGRES_PASSWORD {message}" in error
    if value:
        assert value not in error


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("MINIO_ACCESS_KEY", "", "must be non-empty"),
        ("MINIO_SECRET_KEY", "", "must be non-empty"),
        ("MINIO_ACCESS_KEY", "minioadmin", "does not satisfy secret policy"),
        ("MINIO_SECRET_KEY", "short", "does not satisfy secret policy"),
    ],
)
def test_check_compose_storage_rejects_invalid_credentials(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, key: str, value: str, message: str
) -> None:
    monkeypatch.delenv("ALLOW_INSECURE_SECRETS", raising=False)
    monkeypatch.setenv("MINIO_ACCESS_KEY", "openrag123456")
    monkeypatch.setenv("MINIO_SECRET_KEY", "long-secret-value-123456")
    monkeypatch.setenv(key, value)

    assert gen_env.main(["--check-compose-storage"]) == 1
    error = capsys.readouterr().err
    assert f"{key} {message}" in error
    if value:
        assert value not in error


def test_check_allows_empty_postgres_password_when_template_does_not_use_it(tmp_path: Path) -> None:
    template = _write(tmp_path / ".env.example", "OTHER_SETTING=present\n")
    output = _write(tmp_path / ".env", "OTHER_SETTING=present\nPOSTGRES_PASSWORD=\n")

    assert gen_env.main(["--check", "-t", str(template), "-o", str(output)]) == 0


@pytest.mark.parametrize(
    ("key", "value"),
    [("POSTGRES_PASSWORD", "postgres"), ("GRAFANA_ADMIN_PASSWORD", "minioadmin")],
)
def test_check_fails_on_shared_secret_policy_violations(
    tmp_path: Path, capsys: pytest.CaptureFixture, key: str, value: str
) -> None:
    template = _write(tmp_path / ".env.example", f"{key}=__GENERATE_ME__\n")
    output = _write(tmp_path / ".env", f"{key}={value}\n")
    before = output.read_text()

    rc = gen_env.main(["--check", "-t", str(template), "-o", str(output)])

    assert rc == 1
    error = capsys.readouterr().err
    assert key in error
    assert value not in error
    assert output.read_text() == before, "--check must not modify the environment file"


@pytest.mark.parametrize(
    "assignment",
    [
        "OIDC_CLIENT_SECRET: change-me-the-secret-from-step-1",
        "OIDC_CLIENT_SECRET = change-me-the-secret-from-step-1",
    ],
)
def test_check_applies_secret_policy_to_compose_assignment_syntax(
    tmp_path: Path, capsys: pytest.CaptureFixture, assignment: str
) -> None:
    # OIDC_CLIENT_SECRET is optional and absent from the template, so the
    # policy scan itself must understand Compose's accepted assignment forms.
    template = _write(tmp_path / ".env.example", "OTHER_SETTING=present\n")
    output = _write(tmp_path / ".env", f"OTHER_SETTING=present\n{assignment}\n")

    rc = gen_env.main(["--check", "-t", str(template), "-o", str(output)])

    assert rc == 1
    error = capsys.readouterr().err
    assert "OIDC_CLIENT_SECRET" in error
    assert "change-me-the-secret-from-step-1" not in error


def test_check_uses_last_exported_compose_assignment(tmp_path: Path) -> None:
    template = _write(tmp_path / ".env.example", "AUTH_TOKEN=__GENERATE_ME__\n")
    output = _write(tmp_path / ".env", "AUTH_TOKEN=or-openrag-1234\nexport AUTH_TOKEN=or-generated\n")

    assert gen_env.main(["--check", "-t", str(template), "-o", str(output)]) == 0


@pytest.mark.parametrize(
    ("value", "expected", "needs_interpolation"),
    [
        ("$$literal", "$literal", False),
        ('"\\$literal"', "$literal", False),
        ('"$HOME"', "$HOME", True),
        ("${TOKEN:-fallback}", "${TOKEN:-fallback}", True),
        ("'${TOKEN}'", "${TOKEN}", False),
    ],
)
def test_policy_value_matches_compose_dollar_interpolation(
    value: str, expected: str, needs_interpolation: bool
) -> None:
    assert gen_env._policy_value(value) == (expected, needs_interpolation)


@pytest.mark.parametrize(
    ("escape", "decoded"),
    [(r"\a", "\a"), (r"\b", "\b"), (r"\f", "\f"), (r"\v", "\v")],
)
def test_policy_value_decodes_compose_double_quoted_control_escapes(escape: str, decoded: str) -> None:
    assert gen_env._policy_value(f'"before{escape}after"') == (f"before{decoded}after", False)


def test_double_quoted_backslash_does_not_escape_following_interpolation() -> None:
    assert gen_env._policy_value(r'"\\$RUNTIME_DB_PASSWORD"') == (r"\$RUNTIME_DB_PASSWORD", True)


def test_quote_end_closes_after_a_pair_of_backslashes_before_single_quote() -> None:
    text = "operator_notes='folder\\\\'\nPOSTGRES_PASSWORD=operator-password-1234567890\n"

    values, _, syntax_problems, _ = gen_env._parse_env_assignments(text)

    assert syntax_problems == []
    assert values["operator_notes"][0] == "folder\\\\"
    assert values["POSTGRES_PASSWORD"][0] == "operator-password-1234567890"


def test_check_fails_closed_when_an_assignment_follows_a_quoted_value(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    template = _write(
        tmp_path / ".env.example",
        "POSTGRES_PASSWORD=operator-password-1234567890\nNOTES=ready\n",
    )
    output = _write(
        tmp_path / ".env",
        'POSTGRES_PASSWORD=operator-password-1234567890\nNOTES="ready" POSTGRES_PASSWORD=postgres\n',
    )

    rc = gen_env.main(["--check", "-t", str(template), "-o", str(output)])

    assert rc == 1
    error = capsys.readouterr().err
    assert "NOTES" in error
    assert "unsupported text after its quoted value" in error
    assert "postgres" not in error


def test_quoted_value_still_accepts_a_trailing_comment() -> None:
    values, _, syntax_problems, _ = gen_env._parse_env_assignments('NOTES="ready" # comment\n')

    assert syntax_problems == []
    assert values["NOTES"] == ("ready", False)


def test_check_fails_closed_on_unterminated_multiline_quote(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    template = _write(tmp_path / ".env.example", "OTHER_SETTING=present\n")
    output = _write(
        tmp_path / ".env",
        'OTHER_SETTING="unterminated\nAUTH_TOKEN=__GENERATE_ME__\n',
    )

    rc = gen_env.main(["--check", "-t", str(template), "-o", str(output)])

    assert rc == 1
    error = capsys.readouterr().err
    assert "OTHER_SETTING" in error
    assert "AUTH_TOKEN" not in error, "continuation text must not be parsed as a top-level assignment"


def test_check_accepts_compose_multiline_quotes_and_ignores_embedded_assignments(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    template = _write(
        tmp_path / ".env.example",
        "DESCRIPTION=__GENERATE_ME__\nPOSTGRES_PASSWORD=__GENERATE_ME__\n",
    )
    output = _write(
        tmp_path / ".env",
        "DESCRIPTION='first line\nPOSTGRES_PASSWORD=not-a-real-assignment\nlast line'\n"
        "POSTGRES_PASSWORD=operator-password-1234567890\n",
    )

    assert gen_env.main(["--check", "-t", str(template), "-o", str(output)]) == 0
    assert "no placeholders left" in capsys.readouterr().out


def test_check_does_not_treat_lowercase_multiline_key_continuation_as_assignment(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    template = _write(
        tmp_path / ".env.example",
        "AUTH_TOKEN=__GENERATE_ME__\noperator_notes=__GENERATE_ME__\n",
    )
    output = _write(
        tmp_path / ".env",
        "AUTH_TOKEN=or-openrag-1234\n"
        "operator_notes='first line\nAUTH_TOKEN=or-generated-token-value-123456\nlast line'\n",
    )

    assert gen_env.main(["--check", "-t", str(template), "-o", str(output)]) == 1
    assert "AUTH_TOKEN" in capsys.readouterr().err


def test_bare_key_after_placeholder_fails_closed(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    template = _write(tmp_path / ".env.example", "AUTH_TOKEN=__GENERATE_ME__\n")
    output = _write(tmp_path / ".env", "AUTH_TOKEN=or-generated-token-value-123456\nAUTH_TOKEN\n")

    assert gen_env.main(["--check", "-t", str(template), "-o", str(output)]) == 1
    assert "AUTH_TOKEN" in capsys.readouterr().err


@pytest.mark.parametrize("quote", ["'", '"'])
def test_fill_replaces_quoted_placeholders_and_preserves_comments(monkeypatch: pytest.MonkeyPatch, quote: str) -> None:
    monkeypatch.setattr(gen_env, "_generate", lambda key: f"generated-{key}")

    filled, names = gen_env.fill(f"AUTH_TOKEN={quote}__GENERATE_ME__{quote} # keep this comment\n")

    assert names == ["AUTH_TOKEN"]
    assert filled == f"AUTH_TOKEN={quote}generated-AUTH_TOKEN{quote} # keep this comment\n"


def test_fill_does_not_replace_assignment_looking_text_inside_multiline_quote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gen_env, "_generate", lambda key: f"generated-{key}")
    template = "DESCRIPTION='first line\nAUTH_TOKEN=__GENERATE_ME__\nlast line'\nAUTH_TOKEN=__GENERATE_ME__\n"

    filled, names = gen_env.fill(template)

    assert names == ["AUTH_TOKEN"]
    assert "DESCRIPTION='first line\nAUTH_TOKEN=__GENERATE_ME__\nlast line'" in filled
    assert filled.endswith("AUTH_TOKEN=generated-AUTH_TOKEN\n")


@pytest.mark.parametrize("existing", ["AUTH_TOKEN : 'operator-value'\n", "export AUTH_TOKEN=operator-value\n"])
def test_rerun_recognizes_compose_assignments_before_adding_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing: str
) -> None:
    template = _write(tmp_path / ".env.example", "AUTH_TOKEN=__GENERATE_ME__\n")
    output = _write(tmp_path / ".env", existing)
    monkeypatch.setattr(gen_env, "_generate", lambda key: f"generated-{key}")

    assert gen_env.main(["-t", str(template), "-o", str(output)]) == 0

    assert output.read_text() == existing


def test_rerun_summary_counts_generated_values_before_deduplicating_names(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    template = _write(tmp_path / ".env.example", "AUTH_TOKEN=__GENERATE_ME__\n")
    output = _write(tmp_path / ".env", "AUTH_TOKEN=__GENERATE_ME__\nAUTH_TOKEN=__GENERATE_ME__\n")
    monkeypatch.setattr(gen_env, "_generate", lambda key: f"generated-{key}")

    assert gen_env.main(["-t", str(template), "-o", str(output)]) == 0

    summary = capsys.readouterr().out
    assert "generated 2 missing value(s): AUTH_TOKEN" in summary


def test_check_requires_oidc_secrets_when_oidc_is_enabled(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    template = _write(tmp_path / ".env.example", "AUTH_TOKEN=__GENERATE_ME__\n")
    output = _write(tmp_path / ".env", "AUTH_TOKEN=or-generated\nAUTH_MODE=oidc\n")

    rc = gen_env.main(["--check", "-t", str(template), "-o", str(output)])

    assert rc == 1
    error = capsys.readouterr().err
    for key in (
        "OIDC_ENDPOINT",
        "OIDC_CLIENT_ID",
        "OIDC_CLIENT_SECRET",
        "OIDC_REDIRECT_URI",
        "OIDC_TOKEN_ENCRYPTION_KEY",
    ):
        assert key in error


@pytest.mark.parametrize("mode", ["", "invalid", " OIDC extra "])
def test_check_rejects_invalid_auth_mode(tmp_path: Path, capsys: pytest.CaptureFixture, mode: str) -> None:
    template = _write(tmp_path / ".env.example", "AUTH_TOKEN=__GENERATE_ME__\n")
    output = _write(tmp_path / ".env", f"AUTH_TOKEN=or-generated\nAUTH_MODE={mode}\n")

    assert gen_env.main(["--check", "-t", str(template), "-o", str(output)]) == 1
    assert "AUTH_MODE must be either" in capsys.readouterr().err


def test_add_missing_preserves_template_assignment_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gen_env, "_generate", lambda key: f"generated-{key}")

    text, added, generated = gen_env.add_missing(
        "OTHER_SETTING=kept\n",
        "AUTH_MODE=oidc\nAUTH_MODE=token\nAUTH_TOKEN=__GENERATE_ME__\n",
    )

    assert added == ["AUTH_MODE", "AUTH_TOKEN"]
    assert generated == ["AUTH_TOKEN"]
    assert "AUTH_MODE=oidc\nAUTH_MODE=token" in text
    assert "AUTH_TOKEN=generated-AUTH_TOKEN" in text


def test_generated_oidc_encryption_key_is_valid_fernet_key() -> None:
    generated, names = gen_env.fill("OIDC_TOKEN_ENCRYPTION_KEY=__GENERATE_ME__\n")

    assert names == ["OIDC_TOKEN_ENCRYPTION_KEY"]
    assert (
        gen_env._secret_policy_problems(
            "AUTH_MODE=oidc\nOIDC_ENDPOINT=https://idp.test\nOIDC_CLIENT_ID=client\n"
            "OIDC_REDIRECT_URI=https://openrag.test/callback\nOIDC_CLIENT_SECRET=generated-client-secret\n" + generated
        )
        == []
    )


def test_rerun_summary_counts_generated_credentials_added_from_template(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    template = _write(
        tmp_path / ".env.example",
        "OTHER_SETTING=kept\nAUTH_TOKEN=__GENERATE_ME__\nCHAINLIT_AUTH_SECRET=__GENERATE_ME__\n",
    )
    output = _write(tmp_path / ".env", "EXISTING=operator-value\n")
    monkeypatch.setattr(gen_env, "_generate", lambda key: f"generated-{key}")

    rc = gen_env.main(["-t", str(template), "-o", str(output)])

    assert rc == 0
    summary = capsys.readouterr().out
    assert "generated 2 missing value(s): AUTH_TOKEN, CHAINLIT_AUTH_SECRET" in summary
    assert "Added 3 variable(s)" in summary


def test_rerun_generates_quoted_placeholder_when_adding_missing_variable(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    template = _write(tmp_path / ".env.example", "AUTH_TOKEN='__GENERATE_ME__' # generated auth token\n")
    output = _write(tmp_path / ".env", "OTHER_SETTING=kept\n")
    monkeypatch.setattr(gen_env, "_generate", lambda key: f"generated-{key}")

    assert gen_env.main(["-t", str(template), "-o", str(output)]) == 0

    assert "AUTH_TOKEN='generated-AUTH_TOKEN' # generated auth token" in output.read_text()
    assert "generated 1 missing value(s): AUTH_TOKEN" in capsys.readouterr().out


def test_rerun_does_not_append_assignment_inside_multiline_existing_value(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    template = _write(tmp_path / ".env.example", "DESCRIPTION=__GENERATE_ME__\nPOSTGRES_PASSWORD=__GENERATE_ME__\n")
    output = _write(
        tmp_path / ".env",
        "DESCRIPTION='first line\nPOSTGRES_PASSWORD=not-a-real-assignment\nlast line'\n"
        "POSTGRES_PASSWORD=operator-password-1234567890\n",
    )
    monkeypatch.setattr(gen_env, "_generate", lambda key: f"generated-{key}")

    assert gen_env.main(["-t", str(template), "-o", str(output)]) == 0

    text = output.read_text()
    assert text.count("POSTGRES_PASSWORD=") == 2  # one quoted value line, one real assignment
    assert "generated 0 missing value(s)" in capsys.readouterr().out


def test_rerun_refuses_unterminated_quote_without_writing_partial_results(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    template = _write(tmp_path / ".env.example", "AUTH_TOKEN=__GENERATE_ME__\n")
    original = 'NOTES="unterminated\nAUTH_TOKEN=__GENERATE_ME__\n'
    output = _write(tmp_path / ".env", original)
    monkeypatch.setattr(gen_env, "_generate", lambda key: f"generated-{key}")

    assert gen_env.main(["-t", str(template), "-o", str(output)]) == 1

    assert output.read_text() == original
    assert "cannot safely update" in capsys.readouterr().err


def test_generated_file_is_private(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The output is 0600, and the generator writes it without touching the
    caller's environment.

    The non-export half is asserted against an injected sentinel rather than
    against ``AUTH_TOKEN`` being absent: absence proves nothing about whether
    the script exports, and ``AUTH_TOKEN`` is set in any shell that runs this
    stack — so the previous form failed on a developer machine for a reason
    that had nothing to do with the code.
    """
    sentinel = "sentinel-value-the-generator-must-not-touch"
    monkeypatch.setenv("AUTH_TOKEN", sentinel)
    template = _write(tmp_path / ".env.example", "AUTH_TOKEN=__GENERATE_ME__\n")
    output = tmp_path / ".env"

    gen_env.main(["-t", str(template), "-o", str(output)])

    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert os.environ["AUTH_TOKEN"] == sentinel
    assert sentinel not in output.read_text()  # nor did the ambient value leak in


def test_check_fails_when_the_template_is_missing(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """Without the template the parity half cannot run, so success would report
    a check that did not happen — the same shape as the two bugs above. The
    generate path already refuses a missing template; this matches it."""
    output = _write(tmp_path / ".env", "AUTH_TOKEN=or-generated\n")

    rc = gen_env.main(["--check", "-t", str(tmp_path / "absent.example"), "-o", str(output)])

    assert rc == 1
    assert "cannot verify" in capsys.readouterr().err


def test_add_missing_preserves_assignments_used_by_later_interpolation():
    template = "MODE=oidc\nSAVED_MODE=${MODE}\nMODE=token\nMODE=${MODE}\n"
    text, added, generated = gen_env.add_missing("OTHER_SETTING=kept\n", template)
    assert text.endswith(template)
    assert added == ["MODE", "SAVED_MODE"]
    assert generated == []


@pytest.mark.parametrize("mode", [None, "token"])
def test_add_missing_preserves_assignment_before_a_bare_key(monkeypatch, mode):
    if mode is None:
        monkeypatch.delenv("AUTH_MODE", raising=False)
    else:
        monkeypatch.setenv("AUTH_MODE", mode)
    template = "AUTH_MODE=oidc\nAUTH_MODE\n"
    text, added, generated = gen_env.add_missing("OTHER_SETTING=kept\n", template)
    assert text.endswith(template)
    assert added == ["AUTH_MODE"]
    assert generated == []


@pytest.mark.parametrize(
    ("flag", "key", "value"),
    [
        ("--check-compose-postgres", "POSTGRES_PASSWORD", "postgres"),
        ("--check-compose-storage", "MINIO_ACCESS_KEY", "minioadmin"),
        ("--check-compose-grafana", "GRAFANA_ADMIN_PASSWORD", "admin"),
    ],
)
def test_compose_secret_policy_override_warns_but_preserves_required_values(monkeypatch, capsys, flag, key, value):
    monkeypatch.setenv("MINIO_SECRET_KEY", "long-secret-value-123456")
    monkeypatch.setenv(key, value)
    monkeypatch.delenv("ALLOW_INSECURE_SECRETS", raising=False)

    assert gen_env.main([flag]) == 1
    monkeypatch.setenv("ALLOW_INSECURE_SECRETS", "true")
    assert gen_env.main([flag]) == 0
    assert "warnings" in capsys.readouterr().out
    monkeypatch.setenv(key, "")
    assert gen_env.main([flag]) == 1
