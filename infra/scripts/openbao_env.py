#!/usr/bin/env python3
"""Render a compose ``.env`` from an OpenBao (or HashiCorp Vault) KV v2 secret.

Kubernetes deployments get their secrets through External Secrets Operator
(see ``infra/charts/openrag-stack/values-openbao.yaml``). Docker-compose and
Ansible deployments have no operator, so this script does the equivalent at
deploy time: it reads one KV v2 secret whose keys are OpenRAG environment
variable names and writes them into the ``.env`` file compose loads.

Standard library only — it runs on a bare deployment host with ``python3``.

Usage::

    # Print KEY=value lines to stdout
    BAO_ADDR=https://bao.example.com BAO_TOKEN=... \\
        infra/scripts/openbao_env.py --path secret/openrag/staging

    # Patch the secrets into an existing .env (in place, mode 0600)
    infra/scripts/openbao_env.py --path secret/openrag/staging \\
        --base .env --out .env

    # Build a fresh .env from the example file plus the secrets
    infra/scripts/openbao_env.py --path secret/openrag/staging \\
        --base infra/compose/.env.example --out infra/compose/.env

Environment (``BAO_*`` first, ``VAULT_*`` accepted as a fallback, like the
``bao`` CLI):

    BAO_ADDR         OpenBao URL (required)
    BAO_NAMESPACE    OpenBao namespace (optional; sent as X-Vault-Namespace)
    BAO_TOKEN        A token with read access to the secret, or
    BAO_ROLE_ID /    AppRole credentials — the script logs in itself
    BAO_SECRET_ID    (BAO_APPROLE_MOUNT overrides the ``approle`` mount name)
    BAO_CACERT       Path to a CA bundle for a private TLS authority

Secret *values* are never printed: errors and the summary only name keys.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

ENV_ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Characters every dotenv dialect we feed (docker compose, uv --env-file,
# python-dotenv) reads identically when unquoted. Anything else is quoted.
UNQUOTED_SAFE = re.compile(r"^[A-Za-z0-9_./:@+=,-]+$")
HTTP_TIMEOUT_SECONDS = 15
MARKER = "# --- Managed by infra/scripts/openbao_env.py from {source} ---"


class OpenBaoError(Exception):
    """A failure the operator has to act on. Its message never carries a secret value."""


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


def _request(
    addr: str,
    method: str,
    path: str,
    *,
    token: str | None = None,
    namespace: str | None = None,
    body: dict | None = None,
    cafile: str | None = None,
) -> dict:
    url = addr.rstrip("/") + path
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Vault-Token"] = token
    if namespace:
        headers["X-Vault-Namespace"] = namespace
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    context = ssl.create_default_context(cafile=cafile) if cafile else None
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS, context=context) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        # OpenBao error bodies are {"errors": [...]} and describe the request
        # (permission denied, invalid role...) — never the secret itself.
        detail = ""
        try:
            errors = json.load(exc).get("errors") or []
            detail = "; ".join(str(e) for e in errors)
        except (ValueError, AttributeError):
            pass
        raise OpenBaoError(f"{method} {path}: HTTP {exc.code}{f' ({detail})' if detail else ''}") from None
    except urllib.error.URLError as exc:
        raise OpenBaoError(f"{method} {path}: {exc.reason}") from None
    except ValueError:
        raise OpenBaoError(f"{method} {path}: response is not JSON") from None


def login_approle(
    addr: str,
    role_id: str,
    secret_id: str,
    *,
    mount: str = "approle",
    namespace: str | None = None,
    cafile: str | None = None,
) -> str:
    """Exchange AppRole credentials for a client token."""
    payload = _request(
        addr,
        "POST",
        f"/v1/auth/{mount.strip('/')}/login",
        namespace=namespace,
        body={"role_id": role_id, "secret_id": secret_id},
        cafile=cafile,
    )
    try:
        return payload["auth"]["client_token"]
    except (KeyError, TypeError):
        raise OpenBaoError("AppRole login returned no client_token") from None


def split_kv_path(path: str) -> tuple[str, str]:
    """``secret/openrag/staging`` -> ``("secret", "openrag/staging")``.

    The first segment is the KV v2 mount; the API inserts ``/data/`` after it.
    """
    cleaned = path.strip().strip("/")
    if "/" not in cleaned:
        raise OpenBaoError(f"--path must be <mount>/<secret path>, got {path!r}")
    mount, rest = cleaned.split("/", 1)
    return mount, rest.strip("/")


def read_kv2(
    addr: str,
    token: str,
    path: str,
    *,
    namespace: str | None = None,
    cafile: str | None = None,
) -> dict[str, str]:
    """Read the latest version of a KV v2 secret as a flat ``{key: value}`` mapping."""
    mount, rest = split_kv_path(path)
    payload = _request(addr, "GET", f"/v1/{mount}/data/{rest}", token=token, namespace=namespace, cafile=cafile)
    data = (payload.get("data") or {}).get("data")
    if not isinstance(data, dict):
        raise OpenBaoError(f"no KV v2 data at {path!r} (is the mount really KV version 2?)")
    return {str(key): "" if value is None else str(value) for key, value in data.items()}


# ---------------------------------------------------------------------------
# .env rendering
# ---------------------------------------------------------------------------


def validate_key(key: str) -> None:
    if not ENV_NAME.match(key):
        raise OpenBaoError(f"secret key {key!r} is not a valid environment variable name")


def format_value(key: str, value: str) -> str:
    """Quote a value so compose, uv and python-dotenv all read it back verbatim.

    Single quotes are the one form none of them interpolate or unescape, so
    that is the only quoting used. A value that cannot be single-quoted is
    refused rather than written in a form some parser would mangle silently.
    """
    if "\n" in value or "\r" in value:
        raise OpenBaoError(f"{key}: multi-line values cannot be stored in a .env file")
    if "'" in value:
        raise OpenBaoError(f"{key}: values containing a single quote cannot be stored in a .env file")
    if value and UNQUOTED_SAFE.match(value):
        return value
    return f"'{value}'"


def render_env(secrets: dict[str, str], base: str | None, *, source: str) -> str:
    """Return ``base`` with every ``KEY=`` line for a known secret rewritten, and
    the remaining secrets appended under a marker. With no base, just the secrets."""
    remaining = dict(secrets)
    out: list[str] = []
    for line in (base or "").splitlines():
        match = ENV_ASSIGNMENT.match(line)
        if match and match.group(1) in remaining:
            key = match.group(1)
            out.append(f"{key}={format_value(key, remaining.pop(key))}")
        else:
            out.append(line)
    if remaining:
        if out and out[-1].strip():
            out.append("")
        out.append(MARKER.format(source=source))
        for key in sorted(remaining):
            out.append(f"{key}={format_value(key, remaining[key])}")
    return "\n".join(out) + "\n"


def write_private(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` readable by the owner only, even if the file pre-exists."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(path, 0o600)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _env(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="openbao_env.py",
        description="Render a compose .env from an OpenBao / Vault KV v2 secret.",
        epilog="Auth and address come from BAO_ADDR, BAO_NAMESPACE, BAO_TOKEN or BAO_ROLE_ID/BAO_SECRET_ID.",
    )
    parser.add_argument("--path", required=True, help="KV v2 path including the mount, e.g. secret/openrag/staging")
    parser.add_argument(
        "--base", help="existing .env (or .env.example) to patch; its KEY= lines are rewritten in place"
    )
    parser.add_argument("--out", help="file to write (mode 0600); defaults to stdout. May be the same file as --base")
    parser.add_argument("--only", help="comma-separated subset of keys to take from the secret (default: all)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        addr = _env("BAO_ADDR", "VAULT_ADDR")
        if not addr:
            raise OpenBaoError("BAO_ADDR is not set")
        namespace = _env("BAO_NAMESPACE", "VAULT_NAMESPACE")
        cafile = _env("BAO_CACERT", "VAULT_CACERT")

        token = _env("BAO_TOKEN", "VAULT_TOKEN")
        if not token:
            role_id, secret_id = _env("BAO_ROLE_ID"), _env("BAO_SECRET_ID")
            if not (role_id and secret_id):
                raise OpenBaoError("set BAO_TOKEN, or BAO_ROLE_ID and BAO_SECRET_ID for an AppRole login")
            token = login_approle(
                addr,
                role_id,
                secret_id,
                mount=_env("BAO_APPROLE_MOUNT") or "approle",
                namespace=namespace,
                cafile=cafile,
            )

        secrets = read_kv2(addr, token, args.path, namespace=namespace, cafile=cafile)
        if args.only:
            wanted = [key.strip() for key in args.only.split(",") if key.strip()]
            missing = sorted(set(wanted) - set(secrets))
            if missing:
                raise OpenBaoError(f"keys missing from {args.path}: {', '.join(missing)}")
            secrets = {key: secrets[key] for key in wanted}
        for key in secrets:
            validate_key(key)

        base_text = Path(args.base).read_text(encoding="utf-8") if args.base else None
        text = render_env(secrets, base_text, source=f"{addr} {args.path}")

        if args.out:
            write_private(Path(args.out), text)
            print(
                f"openbao_env: wrote {len(secrets)} keys to {args.out}: {', '.join(sorted(secrets))}", file=sys.stderr
            )
        else:
            sys.stdout.write(text)
        return 0
    except OpenBaoError as exc:
        print(f"openbao_env: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"openbao_env: {exc.strerror or exc}: {exc.filename or ''}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
