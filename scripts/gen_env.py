#!/usr/bin/env python3
"""Turn an example env file into a real one, generating every credential.

``.env.example`` ships ``__GENERATE_ME__`` wherever a credential belongs rather
than a working dev default, because the failure mode this prevents is copying,
not choosing: a file that boots straight after ``cp`` is a file whose
credentials nobody ever changed. That token is on the boot-time denylist, so a
verbatim copy fails loudly at startup instead of installing a known credential.

This script is the other half of that trade — the one command that turns the
example into something that runs:

    python3 scripts/gen_env.py                 # infra/compose/.env
    python3 scripts/gen_env.py --check         # report, change nothing
    python3 scripts/gen_env.py -o /tmp/x.env   # somewhere else

No third-party packages are required, so it runs before any project install.

It is driven entirely by the template: every ``__GENERATE_ME__`` is replaced
with a value chosen from the variable name on that line, so adding a credential
to ``.env.example`` needs no change here. Existing values are never touched, so
re-running it after adding a new secret to the example fills only the gap.
"""

from __future__ import annotations

import argparse
import base64
import importlib
import os
import re
import secrets
import sys
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEMPLATE = REPO_ROOT / "infra/compose/.env.example"
DEFAULT_OUTPUT = REPO_ROOT / "infra/compose/.env"

#: The marker an example file uses where a credential belongs. Also carried on
#: the boot-time denylist in ``openrag/core/config/secrets_guard.py``, so a
#: copied-but-ungenerated file is refused rather than silently accepted.
PLACEHOLDER = "__GENERATE_ME__"

# Compose env-file assignments also permit whitespace, an ``export`` prefix,
# and a colon delimiter; keep the parser shared by checking and generation.
_ENV_FILE_ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*(?:=|:)\s*(?P<value>.*)$")
_ENV_FILE_KEY = re.compile(r"^\s*(?:export\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*(?:=|:|$)")
_INTERPOLATION_START = re.compile(r"[A-Za-z_]")


class _ParsedAssignment(NamedTuple):
    key: str
    value: str
    unresolved: bool
    start_line: int
    end_line: int
    prefix: str
    raw_value: str


def _generate(key: str) -> str:
    """A value in the shape the consumer of ``key`` expects.

    Hex by default: it survives ``.env`` parsing, shell interpolation and
    connection strings without quoting, which matters more here than density.
    """
    if key == "AUTH_TOKEN":
        # Mirrors the format the API issues for user tokens ("or-" + 32 hex).
        return "or-" + secrets.token_hex(16)
    if key == "CHAINLIT_AUTH_SECRET":
        # The value the documentation tells operators to generate.
        return secrets.token_urlsafe(32)
    if key == "OIDC_TOKEN_ENCRYPTION_KEY":
        # Fernet requires 32 random bytes encoded as padded URL-safe base64.
        return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
    if key == "MINIO_ACCESS_KEY":
        # An identifier rather than a secret; keep it recognisable.
        return "openrag" + secrets.token_hex(6)
    return secrets.token_hex(16)


def _write_private(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` so the credentials are never briefly readable.

    ``write_text`` would create the file at 0644 under a 022 umask and only
    narrow it afterwards, and on an overwrite it would put credentials into an
    already-permissive file. Write a 0600 temporary file alongside the target
    and rename it over instead.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # ``mkstemp`` opens a *unique* name with O_EXCL at 0600, so the open cannot
    # land on a path someone else prepared. The previous predictable
    # ``.{name}.tmp`` used O_CREAT without O_EXCL: if that path already existed
    # as a symlink, ``open`` followed it, and the mode argument — which applies
    # only when open() creates the file — was ignored, so the credentials were
    # written into the link's target with whatever permissions it already had.
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        # Renaming over the target replaces the *name*, so a symlink at ``path``
        # is replaced rather than written through.
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def fill(template_text: str) -> tuple[str, list[str]]:
    """Return the filled text and the names of the variables that were filled."""
    _, _, syntax_problems, assignments = _parse_env_assignments(template_text)
    if syntax_problems:
        raise ValueError("cannot safely update env file: " + " ".join(syntax_problems))

    filled: list[str] = []
    out_lines = template_text.splitlines()
    for assignment in assignments:
        if assignment.unresolved or assignment.value != PLACEHOLDER:
            continue
        if assignment.start_line != assignment.end_line:
            raise ValueError(f"cannot safely replace multiline placeholder for {assignment.key}")
        out_lines[assignment.start_line] = assignment.prefix + _replace_placeholder_value(
            assignment.raw_value, _generate(assignment.key)
        )
        filled.append(assignment.key)

    text = "\n".join(out_lines)
    if template_text.endswith("\n"):
        text += "\n"
    return text, filled


def _replace_placeholder_value(raw_value: str, generated: str) -> str:
    """Replace one parsed placeholder while retaining quotes and comments."""
    value = raw_value.lstrip()
    leading = raw_value[: len(raw_value) - len(value)]
    quote = value[0] if value[:1] in ("'", '"') else None
    if quote is not None:
        end = _quote_end(value, quote, start=1)
        if end is None:
            raise ValueError("cannot safely replace an unterminated quoted placeholder")
        return f"{leading}{quote}{generated}{value[end:]}"

    comment_start = value.find(" #")
    if comment_start >= 0:
        core = value[:comment_start].rstrip()
        suffix = value[len(core) :]
    else:
        core = value.rstrip()
        suffix = value[len(core) :]
    return f"{leading}{generated}{suffix}"


def _quote_end(value: str, quote: str, start: int = 0) -> int | None:
    """Find a quote terminator while respecting Compose's escaped quotes."""
    index = start
    while index < len(value):
        if value[index] == "\\":
            run_start = index
            while index < len(value) and value[index] == "\\":
                index += 1
            if index < len(value) and value[index] == quote and (index - run_start) % 2:
                index += 1
            continue
        if value[index] == quote:
            return index
        index += 1
    return None


def _has_unsupported_quoted_tail(value: str) -> bool:
    """Whether non-comment text follows a quoted value on the same line."""
    value = value.strip()
    quote = value[:1]
    if quote not in ("'", '"'):
        return False
    end = _quote_end(value, quote, start=1)
    if end is None:
        return False
    trailing = value[end + 1 :].strip()
    return bool(trailing and not trailing.startswith("#"))


def _compose_dollars(value: str) -> tuple[str, bool]:
    """Unescape Compose's literal-dollar forms and detect actual interpolation."""
    literal: list[str] = []
    interpolates = False
    index = 0
    while index < len(value):
        char = value[index]
        if char != "$":
            literal.append(char)
            index += 1
            continue
        if index + 1 < len(value) and value[index + 1] == "$":
            literal.append("$")
            index += 2
            continue
        if index + 1 < len(value) and (value[index + 1] == "{" or _INTERPOLATION_START.match(value[index + 1])):
            interpolates = True
        literal.append("$")
        index += 1
    return "".join(literal), interpolates


def _policy_value(raw_value: str) -> tuple[str, bool]:
    """Return the literal env-file value and whether Compose must expand it."""
    value = raw_value.strip()

    if value.startswith("'"):
        end = _quote_end(value, "'", start=1)
        if end is None:
            return value, True
        if _has_unsupported_quoted_tail(value):
            return value, True
        return value[1:end].replace("\\'", "'"), False

    if value.startswith('"'):
        end = _quote_end(value, '"', start=1)
        if end is None:
            return value, True
        if _has_unsupported_quoted_tail(value):
            return value, True
        chars: list[str] = []
        escapes = {
            "a": "\a",
            "b": "\b",
            "f": "\f",
            "n": "\n",
            "r": "\r",
            "t": "\t",
            "v": "\v",
            "\\": "\\",
            '"': '"',
        }
        index = 1
        while index < end:
            char = value[index]
            if char == "\\":
                if index + 1 >= end:
                    return value, True
                escaped = value[index + 1]
                if escaped == "$":
                    # Keep an escaped dollar inert through the later Compose
                    # interpolation pass. A doubled source backslash has
                    # already decoded to one literal backslash and must not
                    # escape a following real variable reference.
                    chars.extend(("$", "$"))
                elif escaped in escapes:
                    chars.append(escapes[escaped])
                else:
                    return value, True
                index += 2
                continue
            chars.append(char)
            index += 1
        return _compose_dollars("".join(chars))

    literal = value.split(" #", maxsplit=1)[0].rstrip()
    return _compose_dollars(literal)


def _parse_env_assignments(
    text: str,
) -> tuple[dict[str, tuple[str, bool]], list[str], list[str], list[_ParsedAssignment]]:
    """Parse Compose env-file assignments, including quoted multiline values.

    A line that looks like a key inside a quoted value is content, not another
    assignment. Unterminated quotes are reported and consume the rest of the
    file so malformed input cannot spoof credential checks or template parity.
    """
    values: dict[str, tuple[str, bool]] = {}
    keys: list[str] = []
    syntax_problems: list[str] = []
    assignments: list[_ParsedAssignment] = []
    lines = text.splitlines()
    index = 0

    while index < len(lines):
        line = lines[index]
        key_match = _ENV_FILE_KEY.match(line)
        if key_match is None:
            index += 1
            continue
        key = key_match.group("key")
        keys.append(key)
        match = _ENV_FILE_ASSIGNMENT.match(line)
        if match is None:
            # A bare Compose key inherits from the process environment when
            # present. That external value is not knowable from the env file,
            # and Compose leaves an earlier assignment intact if it is absent.
            prior = values.get(key, ("", False))[0]
            values[key] = (prior, True)
            assignments.append(_ParsedAssignment(key, prior, True, index, index, line, ""))
            index += 1
            continue

        prefix = line[: match.start("value")]
        raw_value = match.group("value")
        quote_candidate = raw_value.lstrip()[:1]
        quote = quote_candidate if quote_candidate in ("'", '"') else None
        start_line = index
        end_line = index
        if quote is not None:
            while _quote_end(raw_value.lstrip(), quote, start=1) is None and end_line + 1 < len(lines):
                end_line += 1
                raw_value += "\n" + lines[end_line]
            if _quote_end(raw_value.lstrip(), quote, start=1) is None:
                values[key] = (raw_value, True)
                syntax_problems.append(f"{key} has an unterminated quoted value; close the quote before end of file.")
                assignments.append(
                    _ParsedAssignment(key, raw_value, True, start_line, len(lines) - 1, prefix, raw_value)
                )
                break

        value, unresolved = _policy_value(raw_value)
        if _has_unsupported_quoted_tail(raw_value):
            syntax_problems.append(
                f"{key} has unsupported text after its quoted value; put each assignment on a separate line."
            )
        values[key] = (value, unresolved)
        assignments.append(_ParsedAssignment(key, value, unresolved, start_line, end_line, prefix, raw_value))
        index = end_line + 1

    return values, keys, syntax_problems, assignments


def _assignment_keys(text: str) -> list[str]:
    """Variable names assigned in ``text`` (commented lines excluded)."""
    return _parse_env_assignments(text)[1]


def find_placeholders(text: str) -> list[str]:
    """Variable names still holding the placeholder."""
    values, _, _, _ = _parse_env_assignments(text)
    return [key for key, value in values.items() if value == (PLACEHOLDER, False)]


def _secret_policy_problems(text: str, *, report: Callable[[str], None] | None = None) -> list[str]:
    """Apply the shared policy to every credential in this multi-service file."""
    policy = _load_secret_policy()

    values, _, syntax_problems, _ = _parse_env_assignments(text)
    problems: list[str] = []

    def add_problem(message: str) -> None:
        problems.append(message)
        if report is not None:
            report(message)

    for problem in syntax_problems:
        add_problem(problem)

    conditional_inputs = {spec.required_when[0] for spec in policy.SECRET_SPECS if spec.required_when is not None}
    policy_env: dict[str, str] = {}
    for key, (literal, unresolved) in values.items():
        if unresolved:
            if key in conditional_inputs:
                add_problem(
                    f"{key} uses Compose interpolation or unsupported quoting. "
                    "Set a literal value before running --check."
                )
        else:
            policy_env[key] = literal

    try:
        auth_mode = policy.parse_auth_mode(policy_env.get("AUTH_MODE", "token"))
    except ValueError:
        auth_mode = None
        add_problem("AUTH_MODE must be either 'token' or 'oidc'. Supply a valid value.")
    if auth_mode == "oidc":
        secret_keys = {spec.env_var for spec in policy.SECRET_SPECS}
        for key in policy.OIDC_REQUIRED_ENV_VARS:
            if key not in secret_keys and not policy_env.get(key, "").strip():
                add_problem(f"{key} must be non-empty for OIDC. Supply a literal value.")

    for spec in policy.SECRET_SPECS:
        value = values.get(spec.env_var)
        if value is not None and value[1]:
            add_problem(
                f"{spec.env_var} uses Compose interpolation or unsupported quoting. "
                "Set a literal value before running --check."
            )
            continue
        literal = value[0] if value is not None else ""
        if policy.secret_value_problem(spec, literal, policy_env):
            # Do not forward a value-dependent policy result to the CLI sink;
            # the variable name and guidance are enough to fix the env file.
            add_problem(f"{spec.env_var} does not satisfy secret policy. Supply a valid value.")
    return problems


def _load_secret_policy():
    """Load the shared policy without requiring the application dependencies."""
    policy_root = str(REPO_ROOT / "openrag")
    inserted_path = policy_root not in sys.path
    if inserted_path:
        sys.path.insert(0, policy_root)
    try:
        return importlib.import_module("core.secret_policy")
    finally:
        if inserted_path:
            sys.path.remove(policy_root)


def _check_compose_secrets(env: Mapping[str, str], keys: tuple[str, ...]) -> int:
    """Report only names and fixed diagnostics for the effective service values."""
    policy = _load_secret_policy()
    allow_insecure = env.get(policy.ALLOW_INSECURE_SECRETS_ENV_VAR, "").strip().lower() == "true"
    failed = False
    warned = False
    for spec in policy.SECRET_SPECS:
        if spec.env_var not in keys:
            continue
        value = env.get(spec.env_var, "")
        if not value.strip():
            print(f"{spec.env_var} must be non-empty for Compose. Supply a value.", file=sys.stderr)
            failed = True
        elif policy.secret_value_problem(spec, value, env):
            print(f"{spec.env_var} does not satisfy secret policy. Supply a valid value.", file=sys.stderr)
            failed |= not allow_insecure
            warned |= allow_insecure
    if failed:
        return 1
    if warned:
        print("ALLOW_INSECURE_SECRETS=true: Compose secret-policy violations are warnings.")
    else:
        print("Compose credentials pass policy.")
    return 0


def add_missing(existing_text: str, template_text: str) -> tuple[str, list[str], list[str]]:
    """Append template assignments ``existing_text`` lacks, preserving what is there.

    Warning about a variable the template gained and returning success left the
    credential unset while the exit code said otherwise — so an upgrade that
    added a required secret looked like it had worked. Generating it is the only
    outcome that makes the documented "re-run after adding a secret" path true.

    Existing assignments are never rewritten: an operator's endpoints and tuning
    survive, and a credential the data was written under is not regenerated.
    """
    _, existing_keys, existing_problems, _ = _parse_env_assignments(existing_text)
    _, _, template_problems, template_assignments = _parse_env_assignments(template_text)
    if existing_problems or template_problems:
        problems = existing_problems + template_problems
        raise ValueError("cannot safely update env file: " + " ".join(problems))

    have = set(existing_keys)
    added: list[str] = []
    generated: list[str] = []
    new_lines: list[str] = []

    template_lines = template_text.splitlines()
    # Compose can resolve references against earlier assignments, including
    # a previous assignment of the same key. Preserve their original order.
    for assignment in template_assignments:
        key = assignment.key
        if key in have:
            continue
        raw_assignment = template_lines[assignment.start_line : assignment.end_line + 1]
        if not assignment.unresolved and assignment.value == PLACEHOLDER:
            if assignment.start_line != assignment.end_line:
                raise ValueError(f"cannot safely replace multiline placeholder for {key}")
            raw_assignment[0] = assignment.prefix + _replace_placeholder_value(assignment.raw_value, _generate(key))
            generated.append(key)
        new_lines.extend(raw_assignment)
        if key not in added:
            added.append(key)

    if not new_lines:
        return existing_text, [], []

    text = existing_text if existing_text.endswith("\n") else existing_text + "\n"
    text += "\n# Added by gen_env.py: variables the template gained since this file was written.\n"
    text += "\n".join(new_lines) + "\n"
    return text, added, generated


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-t", "--template", type=Path, default=DEFAULT_TEMPLATE, help="example env file to read")
    parser.add_argument("-o", "--output", type=Path, default=DEFAULT_OUTPUT, help="env file to write")
    parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="rebuild from the template, regenerating every credential (destructive)",
    )
    check_group = parser.add_mutually_exclusive_group()
    check_group.add_argument(
        "--check",
        action="store_true",
        help="check placeholders, template variables, and secrets; unresolved Compose values fail; writes nothing",
    )
    check_group.add_argument(
        "--check-compose-storage",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    check_group.add_argument(
        "--check-compose-postgres",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    check_group.add_argument("--check-compose-grafana", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    compose_keys = ()
    if args.check_compose_storage:
        compose_keys = ("MINIO_ACCESS_KEY", "MINIO_SECRET_KEY")
    elif args.check_compose_postgres:
        compose_keys = ("POSTGRES_PASSWORD",)
    elif args.check_compose_grafana:
        compose_keys = ("GRAFANA_ADMIN_PASSWORD",)
    if compose_keys:
        return _check_compose_secrets(os.environ, compose_keys)

    if args.check:
        if not args.output.exists():
            print(f"{args.output} does not exist; run this script without --check to create it.", file=sys.stderr)
            return 1
        output_text = args.output.read_text()
        problems = False

        remaining = find_placeholders(output_text)
        if remaining:
            print(
                f"{args.output} still has generated values missing: {', '.join(remaining)}",
                file=sys.stderr,
            )
            problems = True

        # Parity with the template, not just leftover placeholders. A variable
        # the template requires and this file never had holds no placeholder to
        # find, so checking only for markers reported success on a file missing
        # a credential outright.
        if args.template.exists():
            template_text = args.template.read_text()
            _, template_keys, template_syntax_problems, _ = _parse_env_assignments(template_text)
            for problem in template_syntax_problems:
                print(f"{args.template}: {problem}", file=sys.stderr)
                problems = True
            absent = sorted(set(template_keys) - set(_assignment_keys(output_text)))
            if absent:
                print(
                    f"{args.output} is missing variables the template defines: {', '.join(absent)}. "
                    f"Re-run without --check to add them.",
                    file=sys.stderr,
                )
                problems = True
            template_keys_set = set(template_keys)
        else:
            # Not a downgrade to a weaker check: --check exists to verify, and
            # without the template the parity half cannot run at all. Reporting
            # success for a check that did not happen is the failure this whole
            # path was just fixed for. The generate path below already refuses
            # a missing template; this matches it.
            print(
                f"Template not found: {args.template}; cannot verify that every variable is present.",
                file=sys.stderr,
            )
            problems = True
            template_keys_set = set()

        def report_policy_problem(message: str) -> None:
            print(f"{args.output}: {message}", file=sys.stderr)

        if _secret_policy_problems(output_text, report=report_policy_problem):
            problems = True

        output_values, _, _, _ = _parse_env_assignments(output_text)
        for key in ("POSTGRES_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY"):
            if key not in template_keys_set:
                continue
            value = output_values.get(key)
            if value is not None and (value[1] or not value[0].strip()):
                print(f"{args.output}: {key} must be non-empty for Compose. Supply a value.", file=sys.stderr)
                problems = True

        if problems:
            return 1
        print(
            f"{args.output}: no placeholders left, every template variable is present, and configured secrets pass policy."
        )
        return 0

    if not args.template.exists():
        print(f"Template not found: {args.template}", file=sys.stderr)
        return 1

    if args.output.exists() and not args.force:
        # Fill the gaps in what is already there rather than rebuilding from
        # the template: an existing .env holds endpoints and tuning the
        # operator set by hand, and regenerating a credential that data was
        # already written under (a database password, say) breaks the stack.
        template_text = args.template.read_text()
        try:
            text, filled = fill(args.output.read_text())
            text, added, generated_added = add_missing(text, template_text)
        except ValueError as exc:
            print(f"cannot safely update {args.output}: {exc}", file=sys.stderr)
            return 1
        _write_private(args.output, text)
        generated_events = len(filled) + len(generated_added)
        generated_names = list(dict.fromkeys(filled + generated_added))
        print(
            f"Updated {args.output}; generated {generated_events} missing value(s): "
            f"{', '.join(generated_names) or 'none'}"
        )
        if added:
            print(f"Added {len(added)} variable(s) the template defines: {', '.join(added)}")
        return 0

    try:
        text, filled = fill(args.template.read_text())
    except ValueError as exc:
        print(f"cannot safely generate {args.output}: {exc}", file=sys.stderr)
        return 1
    _write_private(args.output, text)
    print(f"Wrote {args.output} with {len(filled)} generated value(s): {', '.join(filled) or 'none'}")
    if not filled:
        print("Nothing was generated — the template holds no placeholders.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
