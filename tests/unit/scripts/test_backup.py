from __future__ import annotations

import importlib.util
import logging
import lzma
import os
import stat
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"


@pytest.fixture
def backup(monkeypatch):
    monkeypatch.syspath_prepend(str(_SCRIPTS))
    spec = importlib.util.spec_from_file_location("backup_script", _SCRIPTS / "backup.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def open_umask():
    previous = os.umask(0o022)
    yield
    os.umask(previous)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
@pytest.mark.parametrize("name", ["backup.openrag", "backup.openrag.xz"])
def test_the_backup_file_is_readable_by_its_owner_only(backup, open_umask, tmp_path, name):
    path = tmp_path / name

    with backup.open_output_file(str(path), logging.getLogger("test")) as out:
        out.write('{"partition": "docs"}\n')

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    opener = lzma.open if name.endswith(".xz") else open
    with opener(path, "rt", encoding="utf-8") as written:
        assert written.read() == '{"partition": "docs"}\n'


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file ownership")
def test_run_as_root_the_file_goes_to_the_owner_of_its_folder(backup, monkeypatch, tmp_path):
    # The documented commands run the backup as root in a container, writing to a
    # mounted host folder: the folder's owner must still be able to read the file.
    folder = tmp_path / "backup"
    folder.mkdir()
    owners = []
    monkeypatch.setattr(backup.os, "geteuid", lambda: 0)
    monkeypatch.setattr(backup.os, "fchown", lambda fd, uid, gid: owners.append((uid, gid)))

    backup.open_output_file(str(folder / "backup.openrag"), logging.getLogger("test")).close()

    assert owners == [(folder.stat().st_uid, folder.stat().st_gid)]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file ownership")
def test_when_the_owner_cannot_be_set_no_file_is_left_behind(backup, monkeypatch, tmp_path):
    def refuse(fd, uid, gid):
        raise PermissionError("chown refused")

    monkeypatch.setattr(backup.os, "geteuid", lambda: 0)
    monkeypatch.setattr(backup.os, "fchown", refuse)
    path = tmp_path / "backup.openrag"

    with pytest.raises(PermissionError):
        backup.open_output_file(str(path), logging.getLogger("test"))

    assert not path.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file ownership")
def test_run_as_another_user_the_ownership_is_left_alone(backup, monkeypatch, tmp_path):
    monkeypatch.setattr(backup.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(backup.os, "fchown", lambda *args: pytest.fail("fchown must not run"))

    backup.open_output_file(str(tmp_path / "backup.openrag"), logging.getLogger("test")).close()


def test_an_existing_file_is_not_overwritten(backup, tmp_path):
    path = tmp_path / "backup.openrag"
    path.write_text("previous backup")

    with pytest.raises(Exception, match="already exists"):
        backup.open_output_file(str(path), logging.getLogger("test"))

    assert path.read_text() == "previous backup"
