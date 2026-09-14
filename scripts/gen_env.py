#!/usr/bin/env python3
"""Turn an example env file into a real one, generating every credential.

``.env.example`` ships ``__GENERATE_ME__`` wherever a credential belongs rather
than a working dev default, because the failure mode this prevents is copying,
not choosing: a file that boots straight after ``cp`` is a file whose
credentials nobody ever changed. That token is on the boot-time denylist, so a
verbatim copy fails loudly at startup instead of installing a known credential.

This script is the other half of that trade — the one command that turns the
example into something that runs:

    uv run python scripts/gen_env.py                 # infra/compose/.env
    uv run python scripts/gen_env.py --check         # report, change nothing
    uv run python scripts/gen_env.py -o /tmp/x.env   # somewhere else

It is driven entirely by the template: every ``__GENERATE_ME__`` is replaced
with a value chosen from the variable name on that line, so adding a credential
to ``.env.example`` needs no change here. Existing values are never touched, so
re-running it after adding a new secret to the example fills only the gap.
"""

from __future__ import annotations

import argparse
import re
import secrets
import sys
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


def find_placeholders(text: str) -> list[str]:
    """Variable names still holding the placeholder."""
    return [
        match.group("key")
        for match in (_ASSIGNMENT.match(line) for line in text.splitlines())
        if match and match.group("value").strip() == PLACEHOLDER
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-t", "--template", type=Path, default=DEFAULT_TEMPLATE, help="example env file to read")
    parser.add_argument("-o", "--output", type=Path, default=DEFAULT_OUTPUT, help="env file to write")
    parser.add_argument("-f", "--force", action="store_true", help="overwrite an existing output file")
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
        remaining = find_placeholders(args.output.read_text())
        if remaining:
            print(
                f"{args.output} still has generated values missing: {', '.join(remaining)}",
                file=sys.stderr,
            )
            return 1
        print(f"{args.output}: no placeholders left.")
        return 0

    if not args.template.exists():
        print(f"Template not found: {args.template}", file=sys.stderr)
        return 1

    if args.output.exists() and not args.force:
        print(
            f"{args.output} already exists. Re-run with --force to replace it "
            f"(this regenerates every credential, which will not match data already "
            f"written under the old ones).",
            file=sys.stderr,
        )
        return 1

    text, filled = fill(args.template.read_text())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(text)
    # Credentials: readable by the owner only, matching what an operator would
    # do by hand.
    args.output.chmod(0o600)

    print(f"Wrote {args.output} with {len(filled)} generated value(s): {', '.join(filled) or 'none'}")
    if not filled:
        print("Nothing was generated — the template holds no placeholders.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
