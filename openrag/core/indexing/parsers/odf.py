"""OpenDocument (ODF) reading shared by the ``.odt`` and ``.odp`` parsers.

An ODF file is a zip package: the document body is ``content.xml``, list styles
may also live in ``styles.xml``, and embedded pictures sit under ``Pictures/``,
referenced from ``draw:image`` elements by ``xlink:href``. This module reads the
XML with ``lxml`` and converts it to Markdown. No office suite is involved.

What is kept: headings, paragraphs, bulleted and numbered lists (nested
included), tables, text boxes, slide titles, speaker notes, and charts, as their
title and data table (``### Chart: <title>``, as the PPTX parser renders them).
What is dropped: character formatting, footnotes and endnotes, comments,
tracked deletions, generated indexes (a table of contents repeats the headings),
and embedded objects other than charts, such as formulas.

Pictures become synthetic ``![](<prefix>-N)`` refs in the Markdown, with one
:class:`ImageBlock` per ref carrying it in ``metadata['markdown_ref']``, as the
DOCX and PPTX parsers do. Captioning is not done here.

Parser-bomb caps mirror the DOCX and PPTX parsers: the XML parts and each
picture are size-checked from the zip header before they are decompressed, and
the slide, picture and table-repeat counts are bounded.
"""

from __future__ import annotations

import html
import logging
import zipfile
import zlib
from collections.abc import Iterator
from io import BytesIO

from ...models.document import ImageBlock
from ..image_preprocessor import UPLOADED_IMAGE_FORMATS, ensure_png_compatible_mode, pil_to_png_bytes

logger = logging.getLogger(__name__)

_MAX_XML_BYTES = 64 * 1024 * 1024
# Each chart is a sub-document with a content.xml of its own, and several frames
# may name the same one: what the charts read is budgeted per document.
_MAX_OBJECT_XML_BYTES = 64 * 1024 * 1024
_MAX_ARCHIVE_ENTRY_BYTES = 100 * 1024 * 1024
_MAX_SLIDES = 2000
_MAX_IMAGES = 2000
# ``table:number-columns-repeated`` and ``number-rows-repeated`` are counts from
# the file: a spreadsheet-style table routinely declares 1024 empty columns.
_MAX_TABLE_COLUMNS = 256
_MAX_TABLE_ROWS = 10_000
# Everything else in the output is bounded by the size of content.xml. Only the
# counts the file declares (``text:c`` on ``text:s``, the table repeats) make
# text out of nothing, so they share one budget per document.
_MAX_REPEATED_CHARS = 1_000_000
# Cells the document's tables may render, after short rows are padded to the
# widest one: padding is made of nothing too, and a row is cheap to write.
_MAX_TABLE_CELLS = 500_000

_NS = {
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
    "table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
    "draw": "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0",
    "presentation": "urn:oasis:names:tc:opendocument:xmlns:presentation:1.0",
    "style": "urn:oasis:names:tc:opendocument:xmlns:style:1.0",
    "xlink": "http://www.w3.org/1999/xlink",
}


def _q(prefix: str, name: str) -> str:
    return f"{{{_NS[prefix]}}}{name}"


_TEXT_H = _q("text", "h")
_TEXT_P = _q("text", "p")
_TEXT_LIST = _q("text", "list")
_TEXT_LIST_ITEM = _q("text", "list-item")
_TEXT_LIST_HEADER = _q("text", "list-header")
_TEXT_LIST_STYLE = _q("text", "list-style")
_TEXT_LEVEL_NUMBER = _q("text", "list-level-style-number")
_TEXT_S = _q("text", "s")
_TEXT_TAB = _q("text", "tab")
_TEXT_LINE_BREAK = _q("text", "line-break")
_TABLE_TABLE = _q("table", "table")
# Containers a table's own rows may sit in. A row inside a nested table is that
# table's, and is read when the cell holding it is.
_TABLE_ROW_CONTAINERS = frozenset(
    {_q("table", "table-header-rows"), _q("table", "table-rows"), _q("table", "table-row-group")}
)
_TABLE_ROW = _q("table", "table-row")
_TABLE_CELL = _q("table", "table-cell")
_TABLE_COVERED_CELL = _q("table", "covered-table-cell")
_DRAW_FRAME = _q("draw", "frame")
_DRAW_IMAGE = _q("draw", "image")
_DRAW_TEXT_BOX = _q("draw", "text-box")
_DRAW_OBJECT = _q("draw", "object")
_DRAW_PAGE = _q("draw", "page")
_DRAW_G = _q("draw", "g")
_PRESENTATION_NOTES = _q("presentation", "notes")
_PRESENTATION_CLASS = _q("presentation", "class")
_XLINK_HREF = _q("xlink", "href")
_CHART_NS = "urn:oasis:names:tc:opendocument:xmlns:chart:1.0"
_CHART_PATH = f"{_q('office', 'body')}/{_q('office', 'chart')}/{{{_CHART_NS}}}chart"
_CHART_TITLE = f"{{{_CHART_NS}}}title"

# Inline content that is not the document's running text.
_SKIPPED_INLINE = frozenset(
    {
        _q("text", "note"),
        _q("office", "annotation"),
        _q("office", "annotation-end"),
        _q("draw", "object"),
        _q("draw", "object-ole"),
    }
)
# Block content left out: generated indexes repeat text found elsewhere, and
# tracked changes hold the deleted text.
_SKIPPED_BLOCKS = _SKIPPED_INLINE | frozenset(
    {
        _q("text", "tracked-changes"),
        _q("text", "table-of-content"),
        _q("text", "illustration-index"),
        _q("text", "table-index"),
        _q("text", "object-index"),
        _q("text", "user-index"),
        _q("text", "alphabetical-index"),
        _q("text", "bibliography"),
        _q("office", "forms"),
        _q("text", "sequence-decls"),
        _q("text", "variable-decls"),
        _q("text", "user-field-decls"),
    }
)
# Slide placeholders that repeat on every slide rather than carry its content.
_SKIPPED_SLIDE_CLASSES = frozenset({"page-number", "footer", "header", "date-time", "page"})


class OdfError(Exception):
    """The package cannot be read as an ODF document."""


def _xml_parser():
    from lxml import etree

    # No entity expansion, DTD loading or network access: the XML is untrusted.
    return etree.XMLParser(resolve_entities=False, load_dtd=False, no_network=True, huge_tree=False)


def _read_xml(package: zipfile.ZipFile, name: str, required: bool):
    from lxml import etree

    try:
        info = package.getinfo(name)
    except KeyError:
        if required:
            raise OdfError(f"missing {name}") from None
        return None
    if info.file_size > _MAX_XML_BYTES:
        raise OdfError(f"{name} is {info.file_size} bytes, over the {_MAX_XML_BYTES} byte limit")
    try:
        data = package.read(name)
    except (zipfile.BadZipFile, zlib.error, RuntimeError, NotImplementedError) as exc:
        if required:
            raise OdfError(f"cannot read {name}: {exc}") from exc
        logger.warning("Ignoring unreadable ODF %s: %s", name, exc)
        return None
    try:
        return etree.fromstring(data, parser=_xml_parser())
    except etree.XMLSyntaxError as exc:
        if required:
            raise OdfError(f"{name} is not well-formed XML: {exc}") from exc
        logger.warning("Ignoring unreadable ODF %s: %s", name, exc)
        return None


class OdfConverter:
    """Convert one open ODF package to Markdown, collecting its pictures."""

    def __init__(self, package: zipfile.ZipFile, image_prefix: str) -> None:
        self._package = package
        self._image_prefix = image_prefix
        self.content = _read_xml(package, "content.xml", required=True)
        self._numbered_levels = self._list_styles()
        self.images: list[ImageBlock] = []
        self._page_number = 1
        self._repeat_budget = _MAX_REPEATED_CHARS
        self._cell_budget = _MAX_TABLE_CELLS
        self._object_xml_budget = _MAX_OBJECT_XML_BYTES

    # ----- documents -----

    def text_markdown(self) -> str:
        """The body of an ``.odt``, as one Markdown string."""
        body = self.content.find(f"{_q('office', 'body')}/{_q('office', 'text')}")
        if body is None:
            raise OdfError("content.xml has no office:text body")
        return self._blocks_markdown(body)

    def slides(self) -> Iterator[tuple[int, str]]:
        """``(slide number, Markdown)`` for each slide of an ``.odp``, 1-indexed.

        A slide with no text is skipped but still counted, as in the PPTX parser.
        """
        body = self.content.find(f"{_q('office', 'body')}/{_q('office', 'presentation')}")
        if body is None:
            raise OdfError("content.xml has no office:presentation body")
        for number, page in enumerate(body.iter(_DRAW_PAGE), start=1):
            if number > _MAX_SLIDES:
                logger.warning("Capping ODP slide processing at %d slides", _MAX_SLIDES)
                return
            self._page_number = number
            parts = [part for shape in page if (part := self._shape_markdown(shape))]
            md = "\n".join(parts).strip()
            notes = page.find(_PRESENTATION_NOTES)
            if notes is not None:
                notes_md = "\n".join(
                    self._blocks_markdown(frame)
                    for frame in notes.iter(_DRAW_FRAME)
                    if frame.get(_PRESENTATION_CLASS) == "notes"
                ).strip()
                if notes_md:
                    md = (md + "\n\n### Notes:\n" + notes_md).strip()
            if md:
                yield number, md

    def slide_count(self) -> int:
        body = self.content.find(f"{_q('office', 'body')}/{_q('office', 'presentation')}")
        return 0 if body is None else sum(1 for _ in body.iter(_DRAW_PAGE))

    # ----- slides -----

    def _shape_markdown(self, shape) -> str:
        tag = shape.tag
        if tag == _PRESENTATION_NOTES:
            return ""
        if tag == _DRAW_G:
            return "\n".join(part for child in shape if (part := self._shape_markdown(child)))
        if tag != _DRAW_FRAME:
            # Custom shapes, rectangles, ellipses... hold their text directly.
            return self._blocks_markdown(shape)
        shape_class = shape.get(_PRESENTATION_CLASS)
        if shape_class in _SKIPPED_SLIDE_CLASSES:
            return ""
        if shape_class == "title":
            title = self._blocks_markdown(shape, separator=" ").strip()
            return f"# {title}" if title else ""
        if shape.find(_TABLE_TABLE) is not None:
            return self._table_markdown(shape.find(_TABLE_TABLE))
        return self._frame_markdown(shape)

    # ----- blocks -----

    def _blocks_markdown(self, parent, separator: str = "\n\n") -> str:
        parts = [part for child in parent if (part := self._block_markdown(child))]
        return separator.join(parts)

    def _block_markdown(self, element) -> str:
        tag = element.tag
        if not isinstance(tag, str) or tag in _SKIPPED_BLOCKS:
            return ""
        if tag == _TEXT_H:
            text = self._inline(element).strip()
            if not text:
                return ""
            level = _int_attr(element, _q("text", "outline-level"), 1)
            return "#" * min(max(level, 1), 6) + " " + text
        if tag == _TEXT_P:
            return self._inline(element).strip()
        if tag == _TEXT_LIST:
            return "\n".join(self._list_lines(element, depth=0, style=None))
        if tag == _TABLE_TABLE:
            return self._table_markdown(element)
        if tag == _DRAW_FRAME:
            return self._frame_markdown(element)
        # Sections, text boxes and other containers: their children are blocks.
        return self._blocks_markdown(element)

    def _list_lines(self, element, depth: int, style: str | None) -> list[str]:
        style = element.get(_q("text", "style-name")) or style
        numbered = depth + 1 in self._numbered_levels.get(style or "", frozenset())
        indent = "  " * depth
        lines: list[str] = []
        number = 0
        for item in element:
            if item.tag not in (_TEXT_LIST_ITEM, _TEXT_LIST_HEADER):
                continue
            marker = ""
            if item.tag == _TEXT_LIST_ITEM:
                number += 1
                marker = f"{number}. " if numbered else "- "
            first = True
            for child in item:
                if child.tag == _TEXT_LIST:
                    lines.extend(self._list_lines(child, depth + 1, style))
                    continue
                text = self._block_markdown(child)
                if not text:
                    continue
                prefix = indent + (marker if first else " " * len(marker))
                lines.append(prefix + text.replace("\n", "\n" + indent + " " * len(marker)))
                first = False
        return lines

    def _list_styles(self) -> dict[str, frozenset[int]]:
        """``{list style name: levels numbered rather than bulleted}``."""
        levels: dict[str, frozenset[int]] = {}
        roots = [self.content, _read_xml(self._package, "styles.xml", required=False)]
        for root in roots:
            if root is None:
                continue
            for style in root.iter(_TEXT_LIST_STYLE):
                name = style.get(_q("style", "name"))
                if not name:
                    continue
                levels[name] = frozenset(
                    _int_attr(level, _q("text", "level"), 0)
                    for level in style
                    if level.tag == _TEXT_LEVEL_NUMBER and level.get(_q("style", "num-format")) != ""
                )
        return levels

    # ----- tables -----

    def _table_markdown(self, table) -> str:
        from html_to_markdown import convert

        rows: list[list[str]] = []
        for row in _table_rows(table):
            if len(rows) >= _MAX_TABLE_ROWS:
                logger.warning("Capping ODF table at %d rows", _MAX_TABLE_ROWS)
                break
            cells: list[str] = []
            for cell in row:
                if cell.tag not in (_TABLE_CELL, _TABLE_COVERED_CELL):
                    continue
                text = self._blocks_markdown(cell, separator=" ").replace("\n", " ").strip()
                repeat = _int_attr(cell, _q("table", "number-columns-repeated"), 1)
                cells.extend([text] * self._repeats(text, repeat, _MAX_TABLE_COLUMNS - len(cells)))
                if len(cells) >= _MAX_TABLE_COLUMNS:
                    break
            while cells and not cells[-1]:
                cells.pop()
            repeat = _int_attr(row, _q("table", "number-rows-repeated"), 1)
            # A repeated empty row is filler, kept once and trimmed below.
            rows.extend([cells] * (self._repeats(" ".join(cells), repeat, _MAX_TABLE_ROWS - len(rows)) if cells else 1))
        while rows and not rows[-1]:
            rows.pop()
        if not rows:
            return ""
        width = max(len(row) for row in rows)
        if len(rows) * width > self._cell_budget:
            logger.warning("ODF tables exceed the %d cell budget; truncating", _MAX_TABLE_CELLS)
            rows = rows[: self._cell_budget // width]
            if not rows:
                return ""
        self._cell_budget -= len(rows) * width
        html_rows = ["<html><body><table>"]
        for index, row in enumerate(rows):
            tag = "th" if index == 0 else "td"
            padded = row + [""] * (width - len(row))
            html_rows.append("<tr>" + "".join(f"<{tag}>{html.escape(c)}</{tag}>" for c in padded) + "</tr>")
        html_rows.append("</table></body></html>")
        return convert("".join(html_rows)).strip()

    def _repeats(self, text: str, declared: int, room: int) -> int:
        """How many copies of *text* a declared repeat count may produce.

        At least one, at most *room*, and copies past the first are charged to
        the document's budget. An empty cell costs nothing: it is filler.
        """
        wanted = max(1, min(declared, room))
        if wanted == 1 or not text:
            return wanted
        extra = min(wanted - 1, self._repeat_budget // len(text))
        if extra < wanted - 1:
            logger.warning("ODF repeat counts exceed the %d character budget; truncating", _MAX_REPEATED_CHARS)
        self._repeat_budget -= extra * len(text)
        return 1 + extra

    # ----- inline text and pictures -----

    def _inline(self, element) -> str:
        parts = [element.text or ""]
        for child in element:
            parts.append(self._inline_child(child))
            parts.append(child.tail or "")
        return "".join(parts)

    def _inline_child(self, child) -> str:
        tag = child.tag
        if not isinstance(tag, str) or tag in _SKIPPED_INLINE:
            return ""
        if tag == _TEXT_S:
            return " " * self._repeats(" ", _int_attr(child, _q("text", "c"), 1), _MAX_REPEATED_CHARS)
        if tag == _TEXT_TAB:
            return "\t"
        if tag == _TEXT_LINE_BREAK:
            return "\n"
        if tag == _DRAW_FRAME:
            return self._frame_markdown(child)
        return self._inline(child)

    def _frame_markdown(self, frame) -> str:
        """A frame holds a picture (possibly with fallbacks), a text box, or an object."""
        embedded = frame.find(_DRAW_OBJECT)
        if embedded is not None:
            # Its draw:image siblings are a rendering of the object, not a picture.
            return self._chart_markdown(embedded.get(_XLINK_HREF))
        images = frame.findall(_DRAW_IMAGE)
        if images:
            # Several draw:image children are alternatives of one picture
            # (an SVG and its PNG fallback): keep the first that decodes.
            for image in images:
                ref = self._image_ref(image.get(_XLINK_HREF))
                if ref:
                    return ref
            return ""
        text_box = frame.find(_DRAW_TEXT_BOX)
        if text_box is not None:
            return self._blocks_markdown(text_box, separator="\n")
        return ""

    def _chart_markdown(self, href: str | None) -> str:
        """An embedded chart as ``### Chart: <title>`` and its data table; any other object as nothing."""
        if not href or "://" in href:
            return ""
        name = href.removeprefix("./").rstrip("/") + "/content.xml"
        try:
            size = self._package.getinfo(name).file_size
        except KeyError:
            return ""
        if size > self._object_xml_budget:
            logger.warning("Skipping ODF object %s: embedded objects exceed their XML budget", name)
            return ""
        self._object_xml_budget -= size
        try:
            root = _read_xml(self._package, name, required=False)
        except OdfError as exc:
            logger.warning("Skipping ODF object %s: %s", name, exc)
            return ""
        chart = root.find(_CHART_PATH) if root is not None else None
        if chart is None:
            return ""
        title_element = chart.find(_CHART_TITLE)
        title = "" if title_element is None else self._blocks_markdown(title_element, separator=" ").strip()
        data = chart.find(_TABLE_TABLE)
        table = self._table_markdown(data) if data is not None else ""
        heading = f"### Chart: {title}" if title else "### Chart"
        return f"{heading}\n\n{table}" if table else heading

    def _image_ref(self, href: str | None) -> str:
        if not href or "://" in href:
            return ""
        if len(self.images) >= _MAX_IMAGES:
            return ""
        name = href.removeprefix("./")
        try:
            info = self._package.getinfo(name)
        except KeyError:
            return ""
        if info.file_size > _MAX_ARCHIVE_ENTRY_BYTES:
            logger.warning("Skipping oversized ODF picture %s (%d bytes)", name, info.file_size)
            return ""
        try:
            from PIL import Image

            with Image.open(BytesIO(self._package.read(name)), formats=UPLOADED_IMAGE_FORMATS) as im:
                im = ensure_png_compatible_mode(im)
                png_bytes = pil_to_png_bytes(im)
        except Exception as exc:
            # EMF/WMF/SVG pictures and broken entries: no ref, as DOCX does.
            logger.warning("Skipping unsupported ODF picture %s: %s", name, exc)
            return ""
        ref = f"![]({self._image_prefix}-{len(self.images)})"
        self.images.append(
            ImageBlock(
                image_bytes=png_bytes,
                page_number=self._page_number,
                mime_type="image/png",
                metadata={"markdown_ref": ref},
            )
        )
        return ref


def _table_rows(table) -> Iterator:
    """A table's own rows, in order, without those of tables nested in its cells."""
    for child in table:
        if child.tag == _TABLE_ROW:
            yield child
        elif child.tag in _TABLE_ROW_CONTAINERS:
            yield from _table_rows(child)


def open_odf(path: str) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(path, "r")
    except (zipfile.BadZipFile, OSError) as exc:
        raise OdfError(f"not a readable zip package: {exc}") from exc


def _int_attr(element, name: str, default: int) -> int:
    try:
        return int(element.get(name, default))
    except (TypeError, ValueError):
        return default
