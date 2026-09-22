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

Standard library only, so it runs before any project install.

It is driven entirely by the template: every ``__GENERATE_ME__`` is replaced
with a value chosen from the variable name on that line, so adding a credential
to ``.env.example`` needs no change here. Existing values are never touched, so
re-running it after adding a new secret to the example fills only the gap.
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEMPLATE = REPO_ROOT / "infra/compose/.env.example"
DEFAULT_OUTPUT = REPO_ROOT / "infra/compose/.env"

#: The marker an example file uses where a credential belongs. Also carried on
#: the boot-time denylist in ``openrag/core/config/secrets_guard.py``, so a
#: copied-but-ungenerated file is refused rather than silently accepted.
PLACEHOLDER = "__GENERATE_ME__"

_ASSIGNMENT = re.compile(r"^(?P<key>[A-Z0-9_]+)=(?P<value>.*)$")


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
    filled: list[str] = []
    out_lines: list[str] = []

    for line in template_text.splitlines():
        match = _ASSIGNMENT.match(line)
        if match and match.group("value").strip() == PLACEHOLDER:
            key = match.group("key")
            out_lines.append(f"{key}={_generate(key)}")
            filled.append(key)
        else:
            out_lines.append(line)

    text = "\n".join(out_lines)
    if template_text.endswith("\n"):
        text += "\n"
    return text, filled


def _assignment_keys(text: str) -> list[str]:
    """Variable names assigned in ``text`` (commented lines excluded)."""
    return [m.group("key") for m in (_ASSIGNMENT.match(line) for line in text.splitlines()) if m]


def find_placeholders(text: str) -> list[str]:
    """Variable names still holding the placeholder."""
    return [
        match.group("key")
        for match in (_ASSIGNMENT.match(line) for line in text.splitlines())
        if match and match.group("value").strip() == PLACEHOLDER
    ]


def add_missing(existing_text: str, template_text: str) -> tuple[str, list[str]]:
    """Append template assignments ``existing_text`` lacks, preserving what is there.

    Warning about a variable the template gained and returning success left the
    credential unset while the exit code said otherwise — so an upgrade that
    added a required secret looked like it had worked. Generating it is the only
    outcome that makes the documented "re-run after adding a secret" path true.

    Existing assignments are never rewritten: an operator's endpoints and tuning
    survive, and a credential the data was written under is not regenerated.
    """
    have = set(_assignment_keys(existing_text))
    added: list[str] = []
    new_lines: list[str] = []

    for line in template_text.splitlines():
        match = _ASSIGNMENT.match(line)
        if not match or match.group("key") in have:
            continue
        key = match.group("key")
        if match.group("value").strip() == PLACEHOLDER:
            new_lines.append(f"{key}={_generate(key)}")
        else:
            new_lines.append(line)
        added.append(key)

    if not new_lines:
        return existing_text, []

    text = existing_text if existing_text.endswith("\n") else existing_text + "\n"
    text += "\n# Added by gen_env.py: variables the template gained since this file was written.\n"
    text += "\n".join(new_lines) + "\n"
    return text, added


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
    parser.add_argument(
        "--check",
        action="store_true",
        help="report any variable still holding the placeholder in --output and exit non-zero; writes nothing",
    )
    args = parser.parse_args(argv)

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
            absent = sorted(set(_assignment_keys(args.template.read_text())) - set(_assignment_keys(output_text)))
            if absent:
                print(
                    f"{args.output} is missing variables the template defines: {', '.join(absent)}. "
                    f"Re-run without --check to add them.",
                    file=sys.stderr,
                )
                problems = True
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

        if problems:
            return 1
        print(f"{args.output}: no placeholders left, and every template variable is present.")
        return 0

    if not args.template.exists():
        print(f"Template not found: {args.template}", file=sys.stderr)
        return 1

    if args.output.exists() and not args.force:
        # Fill the gaps in what is already there rather than rebuilding from
        # the template: an existing .env holds endpoints and tuning the
        # operator set by hand, and regenerating a credential that data was
        # already written under (a database password, say) breaks the stack.
        text, filled = fill(args.output.read_text())
        text, added = add_missing(text, args.template.read_text())
        _write_private(args.output, text)
        print(f"Updated {args.output}; generated {len(filled)} missing value(s): {', '.join(filled) or 'none'}")
        if added:
            print(f"Added {len(added)} variable(s) the template defines: {', '.join(added)}")
        return 0

    text, filled = fill(args.template.read_text())
    _write_private(args.output, text)
    print(f"Wrote {args.output} with {len(filled)} generated value(s): {', '.join(filled) or 'none'}")
    if not filled:
        print("Nothing was generated — the template holds no placeholders.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
