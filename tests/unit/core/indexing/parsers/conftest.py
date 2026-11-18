"""Shared image samples for the parser tests."""

from __future__ import annotations

import struct
from collections.abc import Callable
from io import BytesIO

import pytest
from PIL import Image


@pytest.fixture
def encoded_image() -> Callable[[str], bytes]:
    """Encode a small red image in the given Pillow format."""

    def encode(fmt: str) -> bytes:
        buf = BytesIO()
        Image.new("RGB", (40, 40), "red").save(buf, format=fmt)
        return buf.getvalue()

    return encode


@pytest.fixture
def psd_bytes() -> bytes:
    """A valid 2x2 RGB Photoshop file, which Pillow decodes but cannot write."""
    width = height = 2
    header = b"8BPS" + struct.pack(">H6xHIIHH", 1, 3, height, width, 8, 3)
    # Empty color-mode data, image resources and layer sections, then raw pixel data.
    sections = struct.pack(">III", 0, 0, 0) + struct.pack(">H", 0)
    return header + sections + bytes([200]) * (width * height * 3)
