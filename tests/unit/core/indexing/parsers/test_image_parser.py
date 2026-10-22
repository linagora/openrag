"""Unit tests for :class:`ImageParser` raster decoding."""

from __future__ import annotations

from io import BytesIO

import pytest
from core.indexing.parsers.image_parser import ImageParser
from PIL import Image


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "GIF", "WEBP", "BMP"])
def test_upload_image_formats_are_normalized_to_png(fmt, encoded_image):
    png = ImageParser._raster_to_png(encoded_image(fmt))
    assert png is not None
    assert Image.open(BytesIO(png)).format == "PNG"


def test_a_photoshop_file_is_not_decoded(psd_bytes):
    assert ImageParser._raster_to_png(psd_bytes) is None


def test_a_tiff_file_is_not_decoded(encoded_image):
    assert ImageParser._raster_to_png(encoded_image("TIFF")) is None
