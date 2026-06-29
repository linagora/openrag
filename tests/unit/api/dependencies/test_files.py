import io
from pathlib import Path

import pytest
from api.dependencies.files import save_file_to_disk
from core.utils.exceptions import ValidationError
from core.utils.filename import extract_temporal_fields, sanitize_filename
from fastapi import UploadFile


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
async def test_save_file_to_disk_rejects_oversize_upload(tmp_path: Path, monkeypatch):
    # Cap at ~8 bytes; a larger upload must be rejected (413) and not left on disk.
    monkeypatch.setattr("api.dependencies.files.MAX_UPLOAD_SIZE_BYTES", 8)
    upload = UploadFile(file=io.BytesIO(b"x" * 100), filename="big.bin")
    dest_dir = tmp_path / "uploads"

    with pytest.raises(ValidationError) as exc:
        await save_file_to_disk(file=upload, dest_dir=dest_dir, chunk_size=4)

    assert exc.value.status_code == 413
    # The partially written file must have been cleaned up.
    assert not (dest_dir / "big.bin").exists()


@pytest.mark.asyncio
async def test_save_file_to_disk_with_random_prefix(tmp_path, monkeypatch):
    def fake_make_unique_filename(filename: str) -> str:
        assert filename == "test.txt"
        return "PREFIX_1234_test.txt"

    monkeypatch.setattr(
        "api.dependencies.files.make_unique_filename",
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


@pytest.mark.asyncio
async def test_save_file_to_disk_strips_path_components(tmp_path):
    upload = UploadFile(
        filename="../../nested/evil.txt",
        file=io.BytesIO(b"content"),
    )

    saved_path = await save_file_to_disk(file=upload, dest_dir=tmp_path)

    assert saved_path.parent == tmp_path.resolve()
    assert saved_path.name == "evil.txt"
    assert saved_path.read_bytes() == b"content"


@pytest.mark.asyncio
async def test_save_file_to_disk_rejects_empty_filename(tmp_path):
    upload = UploadFile(
        filename="",
        file=io.BytesIO(b"content"),
    )

    with pytest.raises(ValidationError):
        await save_file_to_disk(file=upload, dest_dir=tmp_path)


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


# --- extract_temporal_fields ---


def test_extract_temporal_fields_field_not_in_metadata():
    assert extract_temporal_fields({}, ["created_at"]) == {}


def test_extract_temporal_fields_naive_datetime_defaults_to_utc():
    metadata = {"created_at": "2024-06-15T12:30:00"}
    result = extract_temporal_fields(metadata, ["created_at"])
    assert result == {"created_at": "2024-06-15T12:30:00+00:00"}


def test_extract_temporal_fields_with_timezone():
    metadata = {"created_at": "2024-06-15T12:30:00+02:00"}
    result = extract_temporal_fields(metadata, ["created_at"])
    assert result == {"created_at": "2024-06-15T12:30:00+02:00"}


def test_extract_temporal_fields_invalid_datetime_raises_400():
    with pytest.raises(ValidationError) as exc_info:
        extract_temporal_fields({"created_at": "not-a-date"}, ["created_at"])
    assert exc_info.value.status_code == 400
    assert "not-a-date" in str(exc_info.value)
    assert "created_at" in str(exc_info.value)
