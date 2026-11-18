from __future__ import annotations

import importlib.util
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

    with backup.open_output_file(str(path)) as out:
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
    owner = (folder.stat().st_uid, folder.stat().st_gid)
    owners = []
    stated = []
    real_stat = os.stat
    monkeypatch.setattr(backup.os, "geteuid", lambda: 0)
    monkeypatch.setattr(backup.os, "fchown", lambda fd, uid, gid: owners.append((uid, gid)))
    monkeypatch.setattr(backup.os, "stat", lambda path, *args, **kwargs: stated.append(path) or real_stat(path))

    with backup.open_output_file(str(folder / "backup.openrag")):
        pass

    assert stated == [str(folder)]
    assert owners == [owner]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file ownership")
def test_when_the_owner_cannot_be_set_no_file_is_left_behind(backup, monkeypatch, tmp_path):
    def refuse(fd, uid, gid):
        raise PermissionError("chown refused")

    monkeypatch.setattr(backup.os, "geteuid", lambda: 0)
    monkeypatch.setattr(backup.os, "fchown", refuse)
    path = tmp_path / "backup.openrag"

    with pytest.raises(PermissionError), backup.open_output_file(str(path)):
        pass

    assert not path.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file ownership")
def test_run_as_another_user_the_ownership_is_left_alone(backup, monkeypatch, tmp_path):
    monkeypatch.setattr(backup.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(backup.os, "fchown", lambda *args: pytest.fail("fchown must not run"))

    with backup.open_output_file(str(tmp_path / "backup.openrag")):
        pass


def test_an_existing_file_is_not_overwritten(backup, tmp_path):
    path = tmp_path / "backup.openrag"
    path.write_text("previous backup")

    with pytest.raises(Exception, match="already exists"), backup.open_output_file(str(path)):
        pass

    assert path.read_text() == "previous backup"


@pytest.mark.parametrize("name", ["backup.openrag", "backup.openrag.xz"])
def test_a_failed_backup_leaves_no_file_behind(backup, tmp_path, name):
    # A partial file would make the retry stop at "already exists".
    path = tmp_path / name

    with pytest.raises(RuntimeError, match="milvus went away"), backup.open_output_file(str(path)) as out:
        out.write('{"partition": "docs"}\n')
        raise RuntimeError("milvus went away")

    assert not path.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
@pytest.mark.parametrize("target", ["missing", "victim"])
def test_a_symlink_at_the_output_path_is_refused(backup, tmp_path, target):
    victim = tmp_path / "victim"
    victim.write_text("victim")
    path = tmp_path / "backup.openrag"
    path.symlink_to(tmp_path / target)

    with pytest.raises(Exception, match="already exists"), backup.open_output_file(str(path)):
        pass

    assert path.is_symlink()
    assert victim.read_text() == "victim"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
@pytest.mark.parametrize("name", ["backup.openrag", "backup.openrag.xz"])
def test_the_backup_never_reopens_its_file_by_name(backup, monkeypatch, tmp_path, name):
    # Someone who can write in the folder swaps the new file for a symlink right
    # after it is created: the backup must neither follow it nor remove it.
    victim = tmp_path / "victim"
    victim.write_text("victim")
    path = tmp_path / name

    def swap(fd, file_name):
        os.unlink(file_name)
        os.symlink(victim, file_name)

    monkeypatch.setattr(backup, "_give_to_folder_owner", swap)

    with pytest.raises(RuntimeError, match="milvus went away"), backup.open_output_file(str(path)) as out:
        out.write('{"partition": "docs"}\n')
        raise RuntimeError("milvus went away")

    assert victim.read_text() == "victim"
    assert path.is_symlink()
