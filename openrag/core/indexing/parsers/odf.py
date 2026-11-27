"""OpenDocument (ODF) reading shared by the ``.odt`` and ``.odp`` parsers.

Conversion is done by the ``xberg`` library. This module only fits its output to
the parser→caption contract: pictures become synthetic ``![](<prefix>-N)`` refs,
with one :class:`ImageBlock` per ref carrying it in ``metadata['markdown_ref']``,
as the DOCX and PPTX parsers do. Captioning is not done here.

Left out, as from the other parsers: comments and footnotes, which ``xberg`` writes
as Markdown footnotes whatever ``ContentFilterConfig`` says, so :func:`_patch_content`
takes them out first.

:func:`_patched` rewrites what xberg 1.3 reads wrongly, before it reads it. Each
rewrite has a test, and goes when xberg gets that case right. It is not a parser:
anything it cannot make sense of goes to xberg untouched, to be refused there.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
import zipfile
import zlib
from io import BytesIO
from pathlib import Path
from typing import Any, NamedTuple

from ...models.document import ImageBlock
from ..image_preprocessor import UPLOADED_IMAGE_FORMATS, ensure_png_compatible_mode, pil_to_png_bytes

logger = logging.getLogger(__name__)

_MIME_TYPES = {
    "odt": "application/vnd.oasis.opendocument.text",
    "odp": "application/vnd.oasis.opendocument.presentation",
}
_EXTRACTION_TIMEOUT_SECONDS = 300
# xberg's default ratio limit refuses a whole document over one highly compressible
# entry, such as an embedded OLE object. Its limits on content size still apply.
_MAX_COMPRESSION_RATIO = 100_000

# xberg writes a picture's title as its alt text. Patching takes titles out, but a
# package left unpatched keeps them, and they may hold brackets in pairs. A bracket
# of its own ends the alt text, which keeps matching linear on text full of "![".
_IMAGE_REF = re.compile(r"!\[(?:[^\[\]]|\[[^\[\]]*\])*\]\(image_(\d+)\.\w+\)")
# xberg writes this line before each slide, and as is any text line that reads the same.
_SLIDE_SEPARATOR_LINE = "-----"
_SLIDE_SEPARATOR = re.compile(rf"^{_SLIDE_SEPARATOR_LINE}$", re.MULTILINE)
_MAX_XML_BYTES = 64 * 1024 * 1024
_MAX_PACKAGE_BYTES = 500 * 1024 * 1024
# The only methods zipfile decompresses with an output limit, and the ones ODF
# producers write. It expands the others whole, whatever the header declares.
_BOUNDED_COMPRESSION = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})
_OFFICE = "{urn:oasis:names:tc:opendocument:xmlns:office:1.0}"
_TEXT = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
_DRAW = "{urn:oasis:names:tc:opendocument:xmlns:drawing:1.0}"
_TABLE = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"
_CHART = "{urn:oasis:names:tc:opendocument:xmlns:chart:1.0}"
_SVG = "{urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0}"
_XLINK_HREF = "{http://www.w3.org/1999/xlink}href"
_PRESENTATION = "{urn:oasis:names:tc:opendocument:xmlns:presentation:1.0}"
_PRESENTATION_CLASS = f"{_PRESENTATION}class"
# What charts may add to a document: in XML elements, and in bytes of XML, as much
# as content.xml may hold. Frames may all name one object, so a small file can ask
# for a large table many times over, and one cell can hold megabytes.
_MAX_CHART_ELEMENTS = 500_000
_MAX_CHART_BYTES = _MAX_XML_BYTES
# What parsing charts may cost, all of them together, as each one's tree is kept
# until the document is patched: in XML read, as much as content.xml may hold, and
# in nodes parsed (elements, attributes, comments), as many as they may add.
_MAX_CHART_XML_BYTES = _MAX_XML_BYTES
_MAX_CHART_NODES = _MAX_CHART_ELEMENTS
_CHART_READ_BYTES = 64 * 1024
# Slide placeholders that repeat on every slide rather than carry its content.
_SKIPPED_SLIDE_CLASSES = frozenset({"page-number", "footer", "header", "date-time", "page"})
_NOTES_FORMAT = "odp-speaker-notes"
_MASTER_FORMAT = "odp-master-page"


class OdfError(Exception):
    """The package cannot be read as an ODF document."""


def read_odf(path: str, kind: str) -> Any:
    """Extract the package at *path* with xberg. *kind* is ``"odt"`` or ``"odp"``."""
    import xberg

    config = xberg.ExtractionConfig(
        output_format="markdown",
        escape_markdown=False,
        use_cache=False,
        disable_ocr=True,
        include_document_structure=True,
        extraction_timeout_secs=_EXTRACTION_TIMEOUT_SECONDS,
        images=xberg.ImageExtractionConfig(extract_images=True),
        security_limits=xberg.SecurityLimits(max_compression_ratio=_MAX_COMPRESSION_RATIO),
    )
    source = xberg.ExtractInput(
        kind=xberg.ExtractInputKind.BYTES,
        bytes=_patched(Path(path).read_bytes()),
        mime_type=_MIME_TYPES[kind],
        filename=f"document.{kind}",
    )
    # xberg raises what the file provokes, RuntimeError subclasses for the most part:
    # none of it may fail the indexing task.
    try:
        result = asyncio.run(xberg.extract(source, config=config))
    except Exception as exc:
        raise OdfError(str(exc)) from exc
    if not result.results:
        raise OdfError(str(result.errors))
    return result.results[0]


def _detach_all(elements) -> None:
    """Take each of *elements* out of its parent, leaving the text that follows it behind.

    Each parent's text is joined once: detaching them one by one appends to the
    same string each time, which is quadratic in the number of elements a parent
    holds."""
    removed: dict[Any, set] = {}
    for element in elements:
        parent = element.getparent()
        if parent is not None:
            removed.setdefault(parent, set()).add(element)
    for parent, children in removed.items():
        anchor, pieces = None, [parent.text or ""]
        for child in list(parent):
            if child in children:
                pieces.append(child.tail or "")
                parent.remove(child)
                continue
            if anchor is None:
                parent.text = "".join(pieces) or None
            else:
                anchor.tail = "".join(pieces) or None
            anchor, pieces = child, [child.tail or ""]
        if anchor is None:
            parent.text = "".join(pieces) or None
        else:
            anchor.tail = "".join(pieces) or None


def _patch_content(root, package: zipfile.ZipFile) -> None:
    from lxml import etree

    # Footnotes, endnotes and comments are not indexed.
    _detach_all(list(root.iter(f"{_TEXT}note", f"{_OFFICE}annotation", f"{_OFFICE}annotation-end")))
    # A lone <text:s/> is dropped, which merges the two words around it.
    spaces = list(root.iter(f"{_TEXT}s"))
    for space in spaces:
        space.tail = " " + (space.tail or "")
    _detach_all(spaces)
    # A list header's text is dropped. It is how LibreOffice writes the titles of a
    # deck imported from PowerPoint: an unnumbered paragraph, so that is what it becomes.
    # Each list is scanned once, whatever the number of headers it holds.
    headers: dict[Any, list] = {}
    for header in root.iter(f"{_TEXT}list-header"):
        headers.setdefault(header.getparent(), []).append(header)
    emptied = []
    for list_element, in_list in headers.items():
        if any(child.tag == f"{_TEXT}list-item" for child in list_element):
            for header in in_list:
                header.tag = f"{_TEXT}list-item"
            continue
        for header in in_list:
            for child in list(header):
                list_element.addprevious(child)
            list_element.remove(header)
        if len(list_element) == 0:
            emptied.append(list_element)
    _detach_all(emptied)
    # A shape's text is dropped unless it sits in a frame's text box.
    for shape in list(root.iter(f"{_DRAW}custom-shape", f"{_DRAW}rect", f"{_DRAW}ellipse", f"{_DRAW}polygon")):
        # A comment's tag is a function, not a string.
        text = [child for child in shape if isinstance(child.tag, str) and child.tag.startswith(_TEXT)]
        if not text:
            continue
        box = etree.SubElement(shape, f"{_DRAW}text-box")
        box.extend(text)
        shape.tag = f"{_DRAW}frame"
    # Impress keeps a preview picture of each table in the table's frame, which
    # xberg indexes as a picture of the slide.
    _detach_all(
        [
            preview
            for frame in root.iter(f"{_DRAW}frame")
            if frame.find(f"{_TABLE}table") is not None
            for preview in frame.findall(f"{_DRAW}image")
        ]
    )
    # Notes holding a list are dropped whole.
    for notes in root.iter(f"{_PRESENTATION}notes"):
        etree.strip_tags(notes, f"{_TEXT}list", f"{_TEXT}list-item")
    # A chart renders as one garbled formula: its table goes in its place.
    charts: dict[str, _Chart | None] = {}
    elements, size = _MAX_CHART_ELEMENTS, _MAX_CHART_BYTES
    budget = _ChartBudget()
    inline_frames = []
    for frame in list(root.iter(f"{_DRAW}frame")):
        obj = frame.find(f"{_DRAW}object")
        if obj is None:
            continue
        name = f"{(obj.get(_XLINK_HREF) or '').removeprefix('./').rstrip('/')}/content.xml"
        if name not in charts:
            charts[name] = _chart_table(package, name, budget)
        chart = charts[name]
        if chart and chart.elements <= elements and chart.size <= size:
            elements -= chart.elements
            size -= chart.size
            table = copy.deepcopy(chart.table)
            heading = etree.Element(f"{_TEXT}p")
            heading.text = f"### Chart: {chart.title}" if chart.title else "### Chart"
            for child in list(frame):
                frame.remove(child)
            anchor = frame
            while anchor.getparent().tag in (f"{_TEXT}p", f"{_TEXT}h", f"{_TEXT}span"):
                anchor = anchor.getparent()
            if anchor is frame:
                # On a slide: one frame for the title, one for the table.
                frame.append(table)
                title_frame = etree.Element(f"{_DRAW}frame")
                etree.SubElement(title_frame, f"{_DRAW}text-box").append(heading)
                frame.addprevious(title_frame)
            else:
                inline_frames.append(frame)
                anchor.addnext(table)
                anchor.addnext(heading)
    _detach_all(inline_frames)
    # xberg writes a picture's title, or else its description, as the alt text of its
    # ref, where an unpaired bracket hides the ref, and as text where the picture is,
    # glued to the words around it. That text stays there, as words of its own.
    labels = []
    for frame in root.iter(f"{_DRAW}frame"):
        if frame.find(f"{_DRAW}image") is None:
            continue
        own = [child for child in frame if child.tag in (f"{_SVG}title", f"{_SVG}desc")]
        words = " ".join(" ".join(label.itertext()) for label in own).split()
        if words:
            frame.tail = f" {' '.join(words)} {frame.tail or ''}"
        labels.extend(own)
    _detach_all(labels)
    for page in root.iter(f"{_DRAW}page"):
        title = ""
        skipped = []
        for shape in page:
            shape_class = shape.get(_PRESENTATION_CLASS)
            if shape_class in _SKIPPED_SLIDE_CLASSES:
                skipped.append(shape)
            elif shape_class == "title":
                title = title or " ".join("".join(shape.itertext()).split())
        _detach_all(skipped)
        # A slide's only heading is its name. Naming it after its title shape makes
        # that heading the title, and a slide with no title shape has none.
        page.attrib.pop(f"{_DRAW}name", None)
        if title:
            page.set(f"{_DRAW}name", title)
    etree.cleanup_namespaces(root)


class _Chart(NamedTuple):
    """A chart's data table and title, and what each copy of them costs."""

    table: Any
    title: str
    elements: int
    size: int


class _ChartBudget:
    """What parsing chart objects may still cost, all charts together."""

    def __init__(self) -> None:
        self.xml_bytes = _MAX_CHART_XML_BYTES
        self.nodes = _MAX_CHART_NODES

    def spent(self) -> bool:
        return self.xml_bytes <= 0 or self.nodes <= 0


def _chart_table(package: zipfile.ZipFile, name: str, budget: _ChartBudget) -> _Chart | None:
    """The data table and title of the chart at *name*, if it is one.

    Parsing it is paid from *budget*, and stops when that runs out: no chart is
    parsed past it."""
    from lxml import etree

    if budget.spent():
        return None
    parser = etree.XMLPullParser(events=("start", "comment", "pi"), **_XML_OPTIONS)
    try:
        info = package.getinfo(name)
        if info.file_size > _MAX_XML_BYTES:
            return None
        # Read up to the declared size, as _read does, a chunk at a time.
        with package.open(info) as entry:
            while chunk := entry.read(_CHART_READ_BYTES):
                budget.xml_bytes -= len(chunk)
                parser.feed(chunk)
                budget.nodes -= sum(1 + len(node.attrib) for _, node in parser.read_events())
                if budget.spent():
                    return None
        root = parser.close()
    except (KeyError, zipfile.BadZipFile, zlib.error, RuntimeError, NotImplementedError, etree.XMLSyntaxError):
        return None
    chart = next(root.iter(f"{_CHART}chart"), None)
    table = chart.find(f"{_TABLE}table") if chart is not None else None
    if table is None:
        return None
    # Rows in this wrapper are not read, and a chart's are always in it.
    etree.strip_tags(chart, f"{_TABLE}table-rows")
    title = chart.find(f"{_CHART}title")
    text = " ".join("".join(title.itertext()).split()) if title is not None else ""
    return _Chart(table, text, sum(1 for _ in table.iter()), len(etree.tostring(table)) + len(text.encode()))


def _read(package: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    """The entry *info*, decompressed no further than the size its header declares.

    ``ZipFile.read`` inflates the whole entry before cutting it to that size, so an
    entry declaring less than it holds would cost what it holds."""
    with package.open(info) as entry:
        return entry.read(info.file_size)


# No entity expansion, DTD loading or network access: the XML is untrusted.
_XML_OPTIONS = {"resolve_entities": False, "load_dtd": False, "no_network": True, "huge_tree": False}


def _xml_parser():
    from lxml import etree

    return etree.XMLParser(**_XML_OPTIONS)


def _patched(raw: bytes) -> bytes:
    """*raw* with :func:`_patch_content` applied, or as it is if it cannot be."""
    from lxml import etree

    try:
        with zipfile.ZipFile(BytesIO(raw)) as package:
            infos = package.infolist()
            # From the zip header, before anything is decompressed: xberg refuses these.
            if sum(i.file_size for i in infos) > _MAX_PACKAGE_BYTES:
                return raw
            # Every entry is read below. xberg bounds the other methods itself.
            if any(i.compress_type not in _BOUNDED_COMPRESSION for i in infos):
                return raw
            if package.getinfo("content.xml").file_size > _MAX_XML_BYTES:
                return raw
            root = etree.fromstring(_read(package, package.getinfo("content.xml")), parser=_xml_parser())
            _patch_content(root, package)
            content = etree.tostring(root, xml_declaration=True, encoding="UTF-8")
            out = BytesIO()
            with zipfile.ZipFile(out, "w") as target:
                for info in infos:
                    try:
                        data = content if info.filename == "content.xml" else _read(package, info)
                    except (zipfile.BadZipFile, zlib.error, RuntimeError, NotImplementedError):
                        continue
                    target.writestr(info.filename, data, compress_type=info.compress_type)
            return out.getvalue()
    # Patching is best effort, on untrusted XML of any shape: xberg can still read the original.
    except Exception as exc:
        logger.debug("Not patching ODF package: %s", exc)
        return raw


class _Pictures:
    """Turns xberg's image refs into synthetic ones, collecting the pictures."""

    def __init__(self, extracted: list, prefix: str) -> None:
        self._extracted = extracted
        self._prefix = prefix
        self._seen: set[int] = set()
        self.images: list[ImageBlock] = []

    def rewrite(self, markdown: str, page_number: int) -> str:
        return _IMAGE_REF.sub(lambda match: self._replace(int(match.group(1)), page_number), markdown)

    def _replace(self, index: int, page_number: int) -> str:
        from PIL import Image

        # A picture missing from the package repeats the previous ref instead of
        # taking a number of its own, so a number stands for one picture only once.
        if index >= len(self._extracted) or index in self._seen:
            return ""
        self._seen.add(index)
        try:
            with Image.open(BytesIO(self._extracted[index].data), formats=UPLOADED_IMAGE_FORMATS) as im:
                png_bytes = pil_to_png_bytes(ensure_png_compatible_mode(im))
        except Exception as exc:
            logger.warning("Skipping unsupported ODF picture: %s", exc)
            return ""
        ref = f"![]({self._prefix}-{len(self.images)})"
        self.images.append(
            ImageBlock(
                image_bytes=png_bytes, page_number=page_number, mime_type="image/png", metadata={"markdown_ref": ref}
            )
        )
        return ref


def text_markdown(extracted: Any, image_prefix: str) -> tuple[str, list[ImageBlock]]:
    """The body of an ``.odt``, as one Markdown string, and its pictures."""
    pictures = _Pictures(extracted.images or [], image_prefix)
    return pictures.rewrite(extracted.content or "", 1).strip(), pictures.images


def slides_markdown(extracted: Any, image_prefix: str) -> tuple[int, list[tuple[int, str]], list[ImageBlock]]:
    """An ``.odp``'s slide count, its non-empty slides by number, and its pictures."""
    if extracted.document is None:
        raise OdfError("xberg returned no document structure")
    slide_notes: list[list[str]] = []
    # The lines of each slide's own text that read like a separator: a paragraph
    # holding only that (xberg joins its lines with spaces), or a line of its notes.
    literal_separators: list[int] = []
    master_texts: list[str] = []
    for node in extracted.document.nodes:
        # NodeContent exposes its fields through its JSON form only.
        content = json.loads(str(node.content))
        if content["node_type"] == "slide":
            slide_notes.append([])
            literal_separators.append(0)
        elif content["node_type"] == "paragraph" and slide_notes:
            literal_separators[-1] += " ".join(content.get("text", "").split()) == _SLIDE_SEPARATOR_LINE
        elif content["node_type"] == "raw_block" and content["format"] == _NOTES_FORMAT and slide_notes:
            slide_notes[-1].append(content["content"].strip())
            literal_separators[-1] += content["content"].split("\n").count(_SLIDE_SEPARATOR_LINE)
        elif content["node_type"] == "raw_block" and content["format"] == _MASTER_FORMAT:
            master_texts.append(content["content"])

    body = (extracted.content or "").rstrip()
    # Master pages' text ends the output, after the last slide, and is not its content.
    masters = "".join(master_texts).rstrip()
    if masters and body.endswith(masters):
        body = body[: -len(masters)]

    # Each slide starts at its separator, which its own literal ones follow.
    separators = list(_SLIDE_SEPARATOR.finditer(body))
    if len(separators) == len(slide_notes) + sum(literal_separators):
        starts, at = [], 0
        for literal in literal_separators:
            starts.append(separators[at])
            at += 1 + literal
        sections = [
            body[start.end() : starts[i + 1].start() if i + 1 < len(starts) else len(body)]
            for i, start in enumerate(starts)
        ]
    else:
        # A separator line the structure does not account for: better one block
        # than the wrong slide numbers.
        expected = len(slide_notes) + sum(literal_separators)
        logger.warning("Expected %d ODP slide separators, found %d", expected, len(separators))
        sections = [_SLIDE_SEPARATOR.sub("", body)]
        slide_notes = [[note for notes in slide_notes for note in notes]]

    pictures = _Pictures(extracted.images or [], image_prefix)
    slides: list[tuple[int, str]] = []
    for slide_num, (section, notes) in enumerate(zip(sections, slide_notes, strict=True), start=1):
        md = pictures.rewrite(_slide_markdown(section, notes), slide_num).strip()
        if notes:
            md = f"{md}\n\n### Notes:\n" + "\n".join(notes)
        if md.strip():
            slides.append((slide_num, md.strip()))
    return len(sections), slides, pictures.images


def _slide_markdown(section: str, notes: list[str]) -> str:
    """One slide's body, with its title as ``#`` and its notes taken out."""
    # The notes follow the slide's body, so the last occurrence is the note: a
    # note repeating the title must not take the title out instead.
    for note in reversed(notes):
        if (at := section.rfind(note)) != -1:
            section = section[:at] + section[at + len(note) :]
    section = section.strip()
    if not section.startswith("## "):
        return section
    # xberg writes the name as a heading, then the title shape it was named after.
    name, _, body = section[3:].partition("\n")
    title, _, rest = body.strip().partition("\n")
    rest = rest.strip() if " ".join(title.split()) == " ".join(name.split()) else body.strip()
    return f"# {name.strip()}\n\n{rest}".strip()


__all__ = ["OdfError", "read_odf", "slides_markdown", "text_markdown"]
