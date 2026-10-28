"""Small OpenDocument packages built in memory, for the ODT/ODP tests."""

from __future__ import annotations

import io
import zipfile
from collections.abc import Callable

import pytest

ODF_MIMETYPES = {
    "odt": "application/vnd.oasis.opendocument.text",
    "odp": "application/vnd.oasis.opendocument.presentation",
}

_NAMESPACES = (
    'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
    'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
    'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
    'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
    'xmlns:presentation="urn:oasis:names:tc:opendocument:xmlns:presentation:1.0" '
    'xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" '
    'xmlns:xlink="http://www.w3.org/1999/xlink"'
)

OdfFactory = Callable[..., bytes]


def build_odf(
    kind: str,
    body: str,
    *,
    automatic_styles: str = "",
    styles: str | None = None,
    pictures: dict[str, bytes] | None = None,
    mimetype: str | None = None,
    include_mimetype: bool = True,
    doctype: str = "",
) -> bytes:
    """An ``.odt`` (``kind="odt"``) or ``.odp`` package around *body*.

    *body* is the XML inside ``office:text`` or ``office:presentation``.
    ``mimetype`` is written first and stored, as ODF asks. *doctype* goes
    between the XML declaration and the root element.
    """
    container = "text" if kind == "odt" else "presentation"
    content = (
        f'<?xml version="1.0" encoding="UTF-8"?>{doctype}'
        f'<office:document-content {_NAMESPACES} office:version="1.3">'
        f"<office:automatic-styles>{automatic_styles}</office:automatic-styles>"
        f"<office:body><office:{container}>{body}</office:{container}></office:body>"
        "</office:document-content>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        if include_mimetype:
            zf.writestr(zipfile.ZipInfo("mimetype"), mimetype or ODF_MIMETYPES[kind], compress_type=zipfile.ZIP_STORED)
        zf.writestr("content.xml", content, compress_type=zipfile.ZIP_DEFLATED)
        if styles is not None:
            zf.writestr(
                "styles.xml",
                f'<?xml version="1.0" encoding="UTF-8"?><office:document-styles {_NAMESPACES}>'
                f"<office:styles>{styles}</office:styles></office:document-styles>",
            )
        for name, data in (pictures or {}).items():
            zf.writestr(name, data)
        zf.writestr("META-INF/manifest.xml", "<manifest/>")
    return buf.getvalue()


@pytest.fixture
def odf_package() -> OdfFactory:
    return build_odf
