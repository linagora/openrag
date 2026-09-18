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
