from __future__ import annotations

import lzma
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "entrypoint-backup-mt.sh"

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("bash") is None or shutil.which("xz") is None,
    reason="the script runs in the Linux image, with bash, xz and /proc",
)


@pytest.fixture
def backup_dir(tmp_path):
    folder = tmp_path / "backup"
    folder.mkdir()
    return folder


@pytest.fixture
def output(backup_dir):
    return backup_dir / "docs.openrag.mt.xz"


@pytest.fixture
def run(tmp_path, backup_dir):
    """Run the script against a temporary backup folder, with a stand-in for uv."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text('#!/bin/sh\nprintf \'{"partition": "docs"}\\n\'\nexit "${FAKE_UV_EXIT:-0}"\n')
    uv.chmod(0o755)

    def _run(uv_exit: int = 0) -> subprocess.CompletedProcess:
        env = {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "BACKUP_DIR": str(backup_dir),
            "FAKE_UV_EXIT": str(uv_exit),
        }
        return subprocess.run(["bash", str(_SCRIPT), "docs"], env=env, capture_output=True, text=True, timeout=30)

    return _run


def test_the_backup_is_readable_by_its_owner_only(run, output):
    result = run()

    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert lzma.decompress(output.read_bytes()) == b'{"partition": "docs"}\n'


def test_a_failed_export_leaves_no_file_behind(run, output):
    result = run(uv_exit=1)

    assert result.returncode == 1
    assert not output.exists()


def test_an_existing_file_is_not_overwritten(run, output):
    output.write_text("previous backup")

    result = run()

    assert result.returncode == 1
    assert "already exists" in result.stderr
    assert output.read_text() == "previous backup"


@pytest.mark.parametrize("target", ["missing", "victim", "/dev/null"])
def test_a_symlink_at_the_output_path_is_refused(run, output, tmp_path, target):
    victim = tmp_path / "victim"
    victim.write_text("victim")
    output.symlink_to(target if target.startswith("/") else tmp_path / target)

    result = run()

    assert result.returncode == 1
    assert output.is_symlink()
    assert victim.read_text() == "victim"


def test_an_existing_fifo_is_refused_without_blocking(run, output):
    os.mkfifo(output)

    result = run()

    assert result.returncode == 1
    assert "already exists" in result.stderr
    assert stat.S_ISFIFO(output.lstat().st_mode)


@pytest.mark.skipif(shutil.which("setfacl") is None, reason="needs setfacl")
def test_a_default_acl_on_the_folder_does_not_open_the_backup_to_its_group(run, output, backup_dir):
    acl = subprocess.run(["setfacl", "-d", "-m", "g::r", str(backup_dir)], capture_output=True)
    if acl.returncode != 0:
        pytest.skip("the file system has no ACL support")

    result = run()

    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
