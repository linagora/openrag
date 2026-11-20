"""entrypoint.sh runs one-off commands after the privilege drop (#1066).

`docker compose run --rm openrag uv run ...` must reach the command as the
non-root app user with umask 002: bypassing the entrypoint ran `uv` as root and
left root-owned files in the openrag_venv volume.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
ENTRYPOINT = ROOT / "infra" / "scripts" / "entrypoint.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _shim(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text(f"#!/bin/bash\n{body}\n")
    path.chmod(0o755)


def _run(args: list[str], path_prefix: Path | None = None, extra_env: dict | None = None):
    env = {"PATH": os.environ["PATH"], "APP_UID": "4242"}
    if path_prefix is not None:
        env["PATH"] = f"{path_prefix}:{env['PATH']}"
    env.update(extra_env or {})
    return subprocess.run(
        ["bash", str(ENTRYPOINT), *args], env=env, capture_output=True, text=True, timeout=30, check=False
    )


@pytest.mark.skipif(os.geteuid() == 0, reason="the root branch touches /app")
def test_command_runs_with_group_writable_umask() -> None:
    result = _run(["sh", "-c", "umask; echo ran"])

    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["0002", "ran"]


def test_command_runs_only_after_dropping_root(tmp_path: Path) -> None:
    # Fake `id` reports root until the fake `setpriv` has "dropped" privileges,
    # then re-runs the real script the way the image's /app/entrypoint.sh would.
    state = tmp_path / "dropped"
    _shim(tmp_path, "id", f'[ -e "{state}" ] && echo "$APP_UID" || echo 0')
    _shim(
        tmp_path,
        "setpriv",
        f'echo "setpriv $*" >> "{tmp_path}/log"; touch "{state}"; shift 6; exec bash "{ENTRYPOINT}" "$@"',
    )
    _shim(tmp_path, "mkdir", "true")
    _shim(tmp_path, "chgrp", "true")
    _shim(tmp_path, "chmod", "true")

    result = _run(["sh", "-c", f'echo "cmd as $(id -u)" >> "{tmp_path}/log"'], path_prefix=tmp_path)

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "log").read_text().splitlines() == [
        "setpriv --reuid 4242 --regid 0 --clear-groups /app/entrypoint.sh sh -c "
        f'echo "cmd as $(id -u)" >> "{tmp_path}/log"',
        "cmd as 4242",
    ]


def test_api_image_passes_run_arguments_to_entrypoint() -> None:
    # The shell form (`ENTRYPOINT ../entrypoint.sh`) silently drops the command
    # given to `docker run` / `docker compose run`.
    dockerfile = (ROOT / "infra" / "docker" / "api.Dockerfile").read_text()
    entrypoints = re.findall(r"^ENTRYPOINT (.+)$", dockerfile, re.MULTILINE)

    assert entrypoints == ['["/app/entrypoint.sh"]']


@pytest.mark.parametrize(
    "path",
    [
        "docs/content/docs/documentation/milvus_migration.mdx",
        "docs/content/docs/documentation/sql_migration.mdx",
        *sorted(
            str(p.relative_to(ROOT)) for p in (ROOT / "openrag/services/persistence/migrations/milvus").glob("*.py")
        ),
    ],
)
def test_documented_commands_keep_the_entrypoint(path: str) -> None:
    # Inline `code` in prose may name the flag (to warn against it); commands
    # live in code blocks and docstrings.
    text = re.sub(r"(?<!`)`[^`\n]+`(?!`)", "", (ROOT / path).read_text())

    assert not re.search(r'--entrypoint\s+"(?:"|uv run)', text), (
        f"{path}: overriding the entrypoint runs uv as root (see #1066)"
    )
