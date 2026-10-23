"""Unit tests for :class:`ImageParser` raster decoding."""

from __future__ import annotations

import base64
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


def _svg_with_image(payload: bytes, mime: str) -> bytes:
    href = f"data:{mime};base64,{base64.b64encode(payload).decode()}"
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="40" height="40">'
        f'<image width="40" height="40" xlink:href="{href}"/></svg>'
    ).encode()


def _centre_pixel(png: bytes) -> tuple[int, ...]:
    with Image.open(BytesIO(png)) as image:
        return image.convert("RGB").getpixel((20, 20))


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "GIF", "WEBP", "BMP"])
def test_an_svg_renders_embedded_upload_image_formats(fmt, encoded_image):
    png = ImageParser._svg_to_png(_svg_with_image(encoded_image(fmt), "image/png"))
    red, green, blue = _centre_pixel(png)
    assert red > 200 and green < 50 and blue < 50


def test_an_svg_does_not_decode_an_embedded_tiff(encoded_image):
    png = ImageParser._svg_to_png(_svg_with_image(encoded_image("TIFF"), "image/png"))
    assert png is not None
    assert _centre_pixel(png) != (255, 0, 0)


def test_an_svg_does_not_decode_an_embedded_photoshop_file(psd_bytes):
    png = ImageParser._svg_to_png(_svg_with_image(psd_bytes, "image/png"))
    assert png is not None
    assert _centre_pixel(png) != (200, 200, 200)


def test_a_nested_svg_does_not_decode_an_embedded_tiff(encoded_image):
    inner = _svg_with_image(encoded_image("TIFF"), "image/png")
    png = ImageParser._svg_to_png(_svg_with_image(inner, "image/svg+xml"))
    assert png is not None
    assert _centre_pixel(png) != (255, 0, 0)


def test_an_svg_applies_the_exif_orientation_of_an_embedded_jpeg():
    # Red on the left, blue on the right, tagged to be shown turned 90° clockwise:
    # upright, red is on top and blue at the bottom.
    image = Image.new("RGB", (40, 20), (255, 0, 0))
    image.paste((0, 0, 255), (20, 0, 40, 20))
    exif = Image.Exif()
    exif[0x0112] = 6
    buf = BytesIO()
    image.save(buf, format="JPEG", exif=exif.tobytes(), quality=95)

    png = ImageParser._svg_to_png(_svg_with_image(buf.getvalue(), "image/jpeg"))

    with Image.open(BytesIO(png)) as rendered:
        top, bottom = rendered.convert("RGB").getpixel((20, 5)), rendered.convert("RGB").getpixel((20, 35))
    assert top[0] > 200 and top[2] < 60
    assert bottom[2] > 200 and bottom[0] < 60
