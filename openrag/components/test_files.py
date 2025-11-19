import io
import uuid
from pathlib import Path

from fastapi import UploadFile
import pytest

from components.files import save_file_to_disk


@pytest.mark.asyncio
async def test_save_file_to_disk_writes_content(tmp_path: Path):
    content = b"hello world"
    upload = UploadFile(
        file=io.BytesIO(content),
        filename="test.bin",
    )

    dest_dir = tmp_path / "uploads"

    saved_path = await save_file_to_disk(
        file=upload,
        dest_dir=dest_dir,
        chunk_size=4
    )

    assert saved_path.exists()
    assert saved_path.parent == dest_dir
    assert saved_path.name == "test.bin"

    with open(saved_path, "rb") as f:
        saved_content = f.read()

    assert saved_content == content



@pytest.mark.asyncio
async def test_save_file_to_disk_with_random_prefix(tmp_path, monkeypatch):

    def fake_make_unique_filename(filename: str) -> str:
        assert filename == "test.txt"
        return "PREFIX_1234_test.txt"

    monkeypatch.setattr(
        "components.files.make_unique_filename", fake_make_unique_filename
    )

    file_content = b"hello world"
    upload = UploadFile(
        filename="test.txt",
        file=io.BytesIO(file_content),
    )

    saved_path = await save_file_to_disk(
        file=upload,
        dest_dir=tmp_path,
        chunk_size=1024,
        with_random_prefix=True,
    )

    assert saved_path.parent == tmp_path
    assert saved_path.name == "PREFIX_1234_test.txt"
    assert saved_path.exists()
    assert saved_path.read_bytes() == file_content
