"""Unit tests for ``core.indexing.image_preprocessor``."""

from __future__ import annotations

import base64
import hashlib

from core.indexing.image_preprocessor import (
    MIN_IMAGE_PIXELS,
    decode_data_uri,
    ensure_png_compatible_mode,
    extract_data_uri_image_blocks,
    mime_from_data_uri,
    normalize_data_uri_images,
    pil_to_png_bytes,
)
from PIL import Image


class TestEnsurePngCompatibleMode:
    def test_cmyk_to_rgb(self):
        assert ensure_png_compatible_mode(Image.new("CMYK", (10, 10))).mode == "RGB"

    def test_palette_to_rgba(self):
        assert ensure_png_compatible_mode(Image.new("P", (10, 10))).mode == "RGBA"

    def test_la_to_rgba(self):
        assert ensure_png_compatible_mode(Image.new("LA", (10, 10))).mode == "RGBA"

    def test_rgb_unchanged(self):
        assert ensure_png_compatible_mode(Image.new("RGB", (10, 10))).mode == "RGB"

    def test_rgba_unchanged(self):
        assert ensure_png_compatible_mode(Image.new("RGBA", (10, 10))).mode == "RGBA"


class TestPilToPngBytes:
    def test_rgb_round_trip(self):
        img = Image.new("RGB", (32, 32), "red")
        png = pil_to_png_bytes(img)
        assert png[:8] == b"\x89PNG\r\n\x1a\n"

    def test_cmyk_normalised_then_encoded(self):
        png = pil_to_png_bytes(Image.new("CMYK", (16, 16)))
        assert png[:8] == b"\x89PNG\r\n\x1a\n"

    def test_bytes_passthrough(self):
        raw = b"already-bytes"
        assert pil_to_png_bytes(raw) is raw


class TestDecodeDataUri:
    def test_round_trip(self):
        payload = b"hello"
        uri = f"data:image/png;base64,{base64.b64encode(payload).decode()}"
        assert decode_data_uri(uri) == payload

    def test_malformed_returns_none(self):
        assert decode_data_uri("not-a-data-uri") is None
        assert decode_data_uri("data:image/png;base64,!!!not-base64") is None


class TestMimeFromDataUri:
    def test_jpeg(self):
        assert mime_from_data_uri("data:image/jpeg;base64,xxx") == "image/jpeg"

    def test_png(self):
        assert mime_from_data_uri("data:image/png;base64,xxx") == "image/png"

    def test_malformed_falls_back_to_png(self):
        assert mime_from_data_uri("garbage") == "image/png"


class TestExtractDataUriImageBlocks:
    def _data_uri(self, payload: bytes = b"x", mime: str = "image/png") -> str:
        return f"data:{mime};base64,{base64.b64encode(payload).decode()}"

    def test_emits_one_block_per_match(self):
        uri = self._data_uri(b"hello")
        text = f"intro ![alt-1]({uri}) middle ![alt-2]({uri}) end"
        blocks = extract_data_uri_image_blocks(text, page_number=3)

        assert len(blocks) == 2
        assert all(b.image_bytes == b"hello" for b in blocks)
        assert all(b.page_number == 3 for b in blocks)
        assert blocks[0].metadata["alt"] == "alt-1"
        assert blocks[0].metadata["markdown_ref"] == f"![alt-1]({uri})"

    def test_no_matches_returns_empty(self):
        assert extract_data_uri_image_blocks("plain text") == []
        assert extract_data_uri_image_blocks("") == []

    def test_skips_undecodable(self):
        text = "![](data:image/png;base64,!!!not-base64)"
        assert extract_data_uri_image_blocks(text) == []


class TestNormalizeDataUriImages:
    def _data_uri(self, payload: bytes = b"x", mime: str = "image/png") -> str:
        return f"data:{mime};base64,{base64.b64encode(payload).decode()}"

    def test_extracts_images_and_replaces_payloads_with_safe_refs(self):
        first = self._data_uri(b"first")
        second = self._data_uri(b"second", "image/jpeg")

        text, blocks = normalize_data_uri_images(f"before ![one]({first}) middle ![two]({second}) after")

        assert "data:image" not in text
        assert text.count("openrag-embedded-image-") == 2
        assert [block.image_bytes for block in blocks] == [b"first", b"second"]
        assert all(block.metadata["markdown_ref"] in text for block in blocks)
        assert blocks[0].metadata["markdown_ref"] != blocks[1].metadata["markdown_ref"]

    def test_parameterized_data_uri_is_extracted_and_sanitized(self):
        payload = base64.b64encode(b"svg-image").decode()
        uri = f"data:image/svg+xml;charset=utf-8;base64,{payload}"

        text, blocks = normalize_data_uri_images(f"before ![diagram]({uri}) after")

        assert "data:image" not in text
        assert len(blocks) == 1
        assert blocks[0].image_bytes == b"svg-image"
        assert blocks[0].mime_type == "image/svg+xml"

    def test_generated_reference_does_not_collide_with_existing_markdown(self):
        uri = self._data_uri(b"chart")
        digest_builder = hashlib.sha256(b"1:chart")
        digest_builder.update(b"chart")
        target = f"openrag-embedded-image-{digest_builder.hexdigest()[:16]}"
        existing = f"![chart]({target})"

        text, blocks = normalize_data_uri_images(f"{existing} ![chart]({uri})")

        assert existing in text
        assert len(blocks) == 1
        assert blocks[0].metadata["markdown_ref"] != existing
        assert blocks[0].metadata["markdown_ref"] in text

    def test_malformed_payload_is_removed_even_without_an_image_block(self):
        text, blocks = normalize_data_uri_images("before ![diagram](data:image/png;base64,!!!) after")

        assert text == "before diagram after"
        assert blocks == []

    def test_rejected_payloads_are_still_sanitized(self):
        uri = self._data_uri(b"too-large")

        text, blocks = normalize_data_uri_images(f"before ![large]({uri}) after", max_image_bytes=1)

        assert text == "before large after"
        assert blocks == []

    def test_image_count_limit_does_not_leave_payloads_behind(self):
        uri = self._data_uri()

        text, blocks = normalize_data_uri_images(f"![one]({uri}) ![two]({uri})", max_images=1)

        assert "data:image" not in text
        assert len(blocks) == 1
        assert text.endswith("two")


def test_min_image_pixels_constant():
    assert MIN_IMAGE_PIXELS == 784
