"""Unit tests for ``core.indexing.image_preprocessor``."""

from __future__ import annotations

import base64

from PIL import Image

from .image_preprocessor import (
    MIN_IMAGE_PIXELS,
    decode_data_uri,
    ensure_png_compatible_mode,
    extract_data_uri_image_blocks,
    mime_from_data_uri,
    pil_to_png_bytes,
)


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


def test_min_image_pixels_constant():
    assert MIN_IMAGE_PIXELS == 784
