"""Object keys: encoding, the builders, the limits and parsing back."""

from pathlib import PurePosixPath

import pytest
from core.utils import object_keys as keys
from core.utils.exceptions import InvalidObjectKeyError, ValidationError

UPLOAD_ID = "0123456789abcdef0123456789abcdef"


# --- components --------------------------------------------------------------


@pytest.mark.parametrize("value", ["p", "..", ".", "a/b", "a:b-c_d.e", "é文", "x" * 191])
def test_component_round_trips(value):
    encoded = keys.encode_component(value)
    assert keys.decode_component(encoded) == value


@pytest.mark.parametrize("value", ["..", ".", "a/b", "/", "\\", "a.b"])
def test_component_never_holds_a_separator_or_a_dot(value):
    encoded = keys.encode_component(value)
    assert "/" not in encoded and "." not in encoded and "\\" not in encoded


def test_component_is_canonical():
    # b"A" encodes to "QQ"; "QR" decodes to the same byte with other trailing bits.
    assert keys.encode_component("A") == "QQ"
    with pytest.raises(InvalidObjectKeyError):
        keys.decode_component("QR")


@pytest.mark.parametrize("encoded", ["", "Q", "a=b", "a.b", "a/b", "QQ=="])
def test_decode_refuses_what_encode_never_writes(encoded):
    with pytest.raises(InvalidObjectKeyError):
        keys.decode_component(encoded)


def test_empty_component_is_refused():
    with pytest.raises(InvalidObjectKeyError):
        keys.encode_component("")


def test_component_limit_is_255_encoded_bytes():
    # 191 bytes encode to 255 characters, 192 to 256.
    assert len(keys.encode_component("x" * 191)) == 255
    with pytest.raises(InvalidObjectKeyError):
        keys.encode_component("x" * 192)


def test_invalid_key_error_is_a_400_validation_error():
    error = InvalidObjectKeyError("bad")
    assert isinstance(error, ValidationError)
    assert error.status_code == 400
    assert error.code == "INVALID_OBJECT_KEY"


# --- builders ----------------------------------------------------------------


def test_original_key_shape():
    key = keys.original_key("docs", "report-1", UPLOAD_ID, ".pdf")
    assert key == f"originals/{keys.encode_component('docs')}/{keys.encode_component('report-1')}/{UPLOAD_ID}.pdf"


def test_upload_and_tmp_key_shapes():
    assert keys.upload_key(UPLOAD_ID, ".docx") == f"uploads/{UPLOAD_ID}.docx"
    tmp = keys.tmp_key(".pdf")
    assert tmp.startswith("tmp/") and tmp.endswith(".pdf")
    assert keys.tmp_key() != keys.tmp_key()


def test_suffix_is_optional_and_keeps_its_case():
    assert keys.upload_key(UPLOAD_ID) == f"uploads/{UPLOAD_ID}"
    assert keys.upload_key(UPLOAD_ID, ".PDF").endswith(".PDF")


@pytest.mark.parametrize("file_id", ["..", ".", "a/b", "../../etc"])
def test_a_hostile_file_id_stays_inside_its_prefix(file_id):
    key = keys.original_key("p", file_id, UPLOAD_ID, ".pdf")
    parts = key.split("/")
    assert len(parts) == 4
    assert ".." not in parts and "." not in parts
    assert keys.parse_key(key).file_id == file_id


def test_ids_that_join_alike_get_different_keys():
    a = keys.original_key("a/b", "c", UPLOAD_ID)
    b = keys.original_key("a", "b/c", UPLOAD_ID)
    assert a != b


def test_new_upload_id_is_accepted_and_unique():
    first, second = keys.new_upload_id(), keys.new_upload_id()
    assert first != second
    keys.upload_key(first)


@pytest.mark.parametrize("upload_id", ["", "abc", UPLOAD_ID.upper(), UPLOAD_ID + "0", "../" + UPLOAD_ID[3:]])
def test_malformed_upload_id_is_refused(upload_id):
    with pytest.raises(InvalidObjectKeyError):
        keys.upload_key(upload_id)


@pytest.mark.parametrize("suffix", ["pdf", ".", "..", "./", ".p/f", ".p.f", "." + "a" * 17, ".pdf\n", ".é"])
def test_malformed_suffix_is_refused(suffix):
    with pytest.raises(InvalidObjectKeyError):
        keys.upload_key(UPLOAD_ID, suffix)


def test_validate_suffix():
    assert keys.validate_suffix(".pdf") == ".pdf"
    assert keys.validate_suffix("") == ""
    for bad in ("pdf", "../x", ".a/b", ".a.b", None):
        with pytest.raises(InvalidObjectKeyError):
            keys.validate_suffix(bad)


def test_long_partition_or_file_id_is_refused():
    with pytest.raises(InvalidObjectKeyError):
        keys.original_key("p" * 192, "f", UPLOAD_ID)
    with pytest.raises(InvalidObjectKeyError):
        keys.original_key("p", "f" * 192, UPLOAD_ID)
    # Both at the limit still fit the key limit.
    key = keys.original_key("p" * 191, "f" * 191, UPLOAD_ID, "." + "x" * 16)
    assert len(key.encode()) <= keys.MAX_KEY_BYTES


# --- legacy keys ---------------------------------------------------------------


def test_legacy_key_shape():
    assert keys.legacy_key("ab12_report.pdf") == "legacy/ab12_report.pdf"
    assert keys.legacy_key(PurePosixPath("sub/ab12_report.pdf")) == "legacy/sub/ab12_report.pdf"
    assert keys.legacy_key("ab12_rapport été.pdf") == "legacy/ab12_rapport été.pdf"


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/etc/passwd",
        "../x",
        "a/../b",
        "a/./b",
        "a//b",
        "a/",
        "a\\b",
        "a\x00b",
        "a\nb",
        "originals/x",
        "uploads/x",
        "tmp/x",
        "legacy/x",
        "x" * 256,
    ],
)
def test_legacy_key_refuses_an_unsafe_path(path):
    with pytest.raises(InvalidObjectKeyError):
        keys.legacy_key(path)


def test_legacy_key_refuses_a_key_over_1024_bytes():
    path = "/".join(["d" * 250] * 5)
    assert len(f"legacy/{path}".encode()) > keys.MAX_KEY_BYTES
    with pytest.raises(InvalidObjectKeyError):
        keys.legacy_key(path)


def test_legacy_key_for_source():
    assert keys.legacy_key_for_source("/app/data/ab12_report.pdf", "/app/data") == "legacy/ab12_report.pdf"
    assert keys.legacy_key_for_source(PurePosixPath("/app/data/x/y.pdf"), PurePosixPath("/app/data")) == (
        "legacy/x/y.pdf"
    )


@pytest.mark.parametrize(
    "source",
    ["/tmp/index_url_abc.pdf", "/app/data", "/app/database/x.pdf", "/app/data/../etc/passwd", "relative.pdf"],
)
def test_legacy_key_for_source_outside_data_dir_is_none(source):
    assert keys.legacy_key_for_source(source, "/app/data") is None


# --- markdown sidecars -----------------------------------------------------------


def test_markdown_sidecar_key():
    original = keys.original_key("p", "f", UPLOAD_ID, ".pdf")
    sidecar = keys.markdown_sidecar_key(original)
    assert sidecar == original + ".md"
    parsed = keys.parse_key(sidecar)
    assert parsed.markdown_sidecar and parsed.suffix == ".pdf"
    assert keys.markdown_sidecar_key("legacy/ab12_report.pdf") == "legacy/ab12_report.pdf.md"


def test_a_markdown_upload_is_not_a_sidecar():
    parsed = keys.parse_key(keys.upload_key(UPLOAD_ID, ".md"))
    assert parsed.suffix == ".md" and not parsed.markdown_sidecar
    sidecar = keys.parse_key(keys.markdown_sidecar_key(keys.upload_key(UPLOAD_ID, ".md")))
    assert sidecar.suffix == ".md" and sidecar.markdown_sidecar


@pytest.mark.parametrize(
    "key",
    [
        keys.tmp_key(".pdf"),
        keys.upload_key(UPLOAD_ID),
        keys.markdown_sidecar_key(keys.upload_key(UPLOAD_ID, ".pdf")),
    ],
)
def test_markdown_sidecar_key_refuses(key):
    with pytest.raises(InvalidObjectKeyError):
        keys.markdown_sidecar_key(key)


# --- parsing ---------------------------------------------------------------------


def test_parse_original_key():
    parsed = keys.parse_key(keys.original_key("docs", "..", UPLOAD_ID, ".pdf"))
    assert parsed == keys.ParsedKey(
        prefix="originals", partition="docs", file_id="..", upload_id=UPLOAD_ID, suffix=".pdf"
    )


def test_parse_upload_tmp_and_legacy_keys():
    assert keys.parse_key(keys.upload_key(UPLOAD_ID, ".mp3")) == keys.ParsedKey(
        prefix="uploads", upload_id=UPLOAD_ID, suffix=".mp3"
    )
    tmp = keys.parse_key(keys.tmp_key())
    assert tmp.prefix == "tmp" and tmp.suffix == "" and tmp.upload_id is not None
    assert keys.parse_key("legacy/a/b.pdf") == keys.ParsedKey(prefix="legacy", relative_path="a/b.pdf")


@pytest.mark.parametrize(
    "key",
    [
        "",
        "/",
        "originals",
        "objects/x",
        "/uploads/" + UPLOAD_ID,
        "uploads/" + UPLOAD_ID + "/",
        "uploads//" + UPLOAD_ID,
        "uploads/x/" + UPLOAD_ID,
        "uploads/" + UPLOAD_ID + ".p/df",
        "tmp/" + UPLOAD_ID + ".pdf.md",
        "originals/cA/" + UPLOAD_ID,
        "originals/cA/ZA/../" + UPLOAD_ID,
        "originals/cA/ZA/x/" + UPLOAD_ID,
        "originals/cA==/ZA/" + UPLOAD_ID,
        "originals/../ZA/" + UPLOAD_ID,
        "legacy/",
        "legacy/../x",
        "legacy/tmp/x",
        "legacy/" + "x" * 1020,
    ],
)
def test_parse_refuses_a_key_no_builder_makes(key):
    with pytest.raises(InvalidObjectKeyError):
        keys.parse_key(key)


def test_validate_key_returns_the_key():
    key = keys.original_key("p", "f", UPLOAD_ID, ".pdf")
    assert keys.validate_key(key) == key
    with pytest.raises(InvalidObjectKeyError):
        keys.validate_key("../etc/passwd")
