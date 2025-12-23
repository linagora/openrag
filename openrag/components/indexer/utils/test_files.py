import io
from pathlib import Path

import pytest
from fastapi import UploadFile

from .files import sanitize_filename, save_file_to_disk


@pytest.mark.asyncio
async def test_save_file_to_disk_writes_content(tmp_path: Path):
    content = b"hello world"
    upload = UploadFile(
        file=io.BytesIO(content),
        filename="test.bin",
    )

    dest_dir = tmp_path / "uploads"

    saved_path = await save_file_to_disk(file=upload, dest_dir=dest_dir, chunk_size=4)

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
        "openrag.components.indexer.utils.files.make_unique_filename",
        fake_make_unique_filename,
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


@pytest.mark.parametrize(
    "input_name,expected",
    [
        # Basic cases
        ("simple_file.txt", "simple_file.txt"),
        ("file-name.pdf", "file_name.pdf"),
        # Spaces and commas
        ("my file.txt", "my_file.txt"),
        ("file,name.txt", "file_name.txt"),
        ("multiple   spaces.txt", "multiple_spaces.txt"),
        # Special characters
        ("file@name#2024.txt", "file_name_2024.txt"),
        ("doc$with%special&chars.pdf", "doc_with_special_chars.pdf"),
        # Multiple underscores
        ("file___name.txt", "file_name.txt"),
        ("file__name__here.txt", "file_name_here.txt"),
        # Edge cases
        ("", ""),
        ("file(1).txt", "file_1.txt"),
        ("file.with.dot.txt", "file_with_dot.txt"),
    ],
)
def test_sanitize_filename(input_name, expected):
    assert sanitize_filename(input_name) == expected
