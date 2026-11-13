"""OpenDocument (ODF) reading shared by the ``.odt`` and ``.odp`` parsers.

Conversion is done by the ``xberg`` library. This module only fits its output to
the parser→caption contract: pictures become synthetic ``![](<prefix>-N)`` refs,
with one :class:`ImageBlock` per ref carrying it in ``metadata['markdown_ref']``,
as the DOCX and PPTX parsers do. Captioning is not done here.

Left out, as from the other parsers: comments and footnotes, which ``xberg`` writes
as Markdown footnotes whatever ``ContentFilterConfig`` says.

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
from typing import Any

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

_IMAGE_REF = re.compile(r"!\[[^\]]*\]\(image_(\d+)\.\w+\)")
_FOOTNOTE_DEFINITION = re.compile(r"^\[\^[^\]\n]+\]:\n(?:(?: {4}.*)?\n?)*", re.MULTILINE)
_FOOTNOTE_REF = re.compile(r"\[\^[^\]\n]+\]")
_SLIDE_SEPARATOR = re.compile(r"^-----$", re.MULTILINE)
# Impress stores a preview picture beside every table, which xberg takes for a
# picture of the slide. It renders straight after the table.
_TABLE_PREVIEW = re.compile(r"(?<=\|\n\n)!\[[^\]]*\]\(image_\d+\.\w+\)\n?")
_MAX_XML_BYTES = 64 * 1024 * 1024
_MAX_PACKAGE_BYTES = 500 * 1024 * 1024
_TEXT = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
_DRAW = "{urn:oasis:names:tc:opendocument:xmlns:drawing:1.0}"
_TABLE = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"
_CHART = "{urn:oasis:names:tc:opendocument:xmlns:chart:1.0}"
_XLINK_HREF = "{http://www.w3.org/1999/xlink}href"
_PRESENTATION = "{urn:oasis:names:tc:opendocument:xmlns:presentation:1.0}"
_PRESENTATION_CLASS = f"{_PRESENTATION}class"
# What charts may add to a document, in XML elements. Frames may all name one object,
# so a small file can ask for a large table many times over.
_MAX_CHART_ELEMENTS = 500_000
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


def _detach(element) -> None:
    """Take *element* out of its parent, leaving the text that follows it behind."""
    parent = element.getparent()
    tail, element.tail = element.tail, None
    if tail:
        previous = element.getprevious()
        if previous is not None:
            previous.tail = (previous.tail or "") + tail
        else:
            parent.text = (parent.text or "") + tail
    parent.remove(element)


def _patch_content(root, package: zipfile.ZipFile) -> None:
    from lxml import etree

    # A lone <text:s/> is dropped, which merges the two words around it.
    for space in list(root.iter(f"{_TEXT}s")):
        space.tail = " " + (space.tail or "")
        _detach(space)
    # A list header's text is dropped. It is how LibreOffice writes the titles of a
    # deck imported from PowerPoint: an unnumbered paragraph, so that is what it becomes.
    for header in list(root.iter(f"{_TEXT}list-header")):
        list_element = header.getparent()
        if any(child.tag == f"{_TEXT}list-item" for child in list_element):
            header.tag = f"{_TEXT}list-item"
            continue
        for child in list(header):
            list_element.addprevious(child)
        list_element.remove(header)
        if len(list_element) == 0:
            _detach(list_element)
    # A shape's text is dropped unless it sits in a frame's text box.
    for shape in list(root.iter(f"{_DRAW}custom-shape", f"{_DRAW}rect", f"{_DRAW}ellipse", f"{_DRAW}polygon")):
        # A comment's tag is a function, not a string.
        text = [child for child in shape if isinstance(child.tag, str) and child.tag.startswith(_TEXT)]
        if not text:
            continue
        box = etree.SubElement(shape, f"{_DRAW}text-box")
        box.extend(text)
        shape.tag = f"{_DRAW}frame"
    # Notes holding a list are dropped whole.
    for notes in root.iter(f"{_PRESENTATION}notes"):
        etree.strip_tags(notes, f"{_TEXT}list", f"{_TEXT}list-item")
    # A chart renders as one garbled formula: its table goes in its place.
    charts: dict[str, Any] = {}
    remaining = _MAX_CHART_ELEMENTS
    for frame in list(root.iter(f"{_DRAW}frame")):
        obj = frame.find(f"{_DRAW}object")
        if obj is None:
            continue
        name = f"{(obj.get(_XLINK_HREF) or '').removeprefix('./').rstrip('/')}/content.xml"
        if name not in charts:
            charts[name] = _chart_table(package, name)
        if charts[name] and charts[name][2] <= remaining:
            table = copy.deepcopy(charts[name][0])
            remaining -= charts[name][2]
            title = charts[name][1]
            heading = etree.Element(f"{_TEXT}p")
            heading.text = f"### Chart: {title}" if title else "### Chart"
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
                _detach(frame)
                anchor.addnext(table)
                anchor.addnext(heading)
    for page in root.iter(f"{_DRAW}page"):
        title = ""
        for shape in list(page):
            shape_class = shape.get(_PRESENTATION_CLASS)
            if shape_class in _SKIPPED_SLIDE_CLASSES:
                _detach(shape)
            elif shape_class == "title":
                title = title or " ".join("".join(shape.itertext()).split())
        # A slide's only heading is its name. Naming it after its title shape makes
        # that heading the title, and a slide with no title shape has none.
        page.attrib.pop(f"{_DRAW}name", None)
        if title:
            page.set(f"{_DRAW}name", title)
    etree.cleanup_namespaces(root)


def _chart_table(package: zipfile.ZipFile, name: str) -> tuple[Any, str, int] | None:
    """The data table, title and element count of the chart at *name*, if it is one."""
    from lxml import etree

    try:
        if package.getinfo(name).file_size > _MAX_XML_BYTES:
            return None
        root = etree.fromstring(package.read(name), parser=_xml_parser())
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
    return table, text, sum(1 for _ in table.iter())


def _xml_parser():
    from lxml import etree

    # No entity expansion, DTD loading or network access: the XML is untrusted.
    return etree.XMLParser(resolve_entities=False, load_dtd=False, no_network=True, huge_tree=False)


def _patched(raw: bytes) -> bytes:
    """*raw* with :func:`_patch_content` applied, or as it is if it cannot be."""
    from lxml import etree

    try:
        with zipfile.ZipFile(BytesIO(raw)) as package:
            infos = package.infolist()
            # From the zip header, before anything is decompressed: xberg refuses these.
            if sum(i.file_size for i in infos) > _MAX_PACKAGE_BYTES:
                return raw
            if package.getinfo("content.xml").file_size > _MAX_XML_BYTES:
                return raw
            root = etree.fromstring(package.read("content.xml"), parser=_xml_parser())
            _patch_content(root, package)
            content = etree.tostring(root, xml_declaration=True, encoding="UTF-8")
            out = BytesIO()
            with zipfile.ZipFile(out, "w") as target:
                for info in infos:
                    try:
                        data = content if info.filename == "content.xml" else package.read(info.filename)
                    except (zipfile.BadZipFile, zlib.error, RuntimeError, NotImplementedError):
                        continue
                    target.writestr(info.filename, data, compress_type=info.compress_type)
            return out.getvalue()
    # Patching is best effort, on untrusted XML of any shape: xberg can still read the original.
    except Exception as exc:
        logger.debug("Not patching ODF package: %s", exc)
        return raw


def _strip_footnotes(markdown: str) -> str:
    return _FOOTNOTE_REF.sub("", _FOOTNOTE_DEFINITION.sub("", markdown))


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
    return pictures.rewrite(_strip_footnotes(extracted.content or ""), 1).strip(), pictures.images


def slides_markdown(extracted: Any, image_prefix: str) -> tuple[int, list[tuple[int, str]], list[ImageBlock]]:
    """An ``.odp``'s slide count, its non-empty slides by number, and its pictures."""
    slide_notes: list[list[str]] = []
    master_text: str | None = None
    for node in extracted.document.nodes:
        # NodeContent exposes its fields through its JSON form only.
        content = json.loads(str(node.content))
        if content["node_type"] == "slide":
            slide_notes.append([])
        elif content["node_type"] == "raw_block" and content["format"] == _NOTES_FORMAT and slide_notes:
            slide_notes[-1].append(content["content"].strip())
        elif content["node_type"] == "raw_block" and content["format"] == _MASTER_FORMAT and master_text is None:
            master_text = content["content"]

    body = _strip_footnotes(extracted.content or "")
    # Master pages come after the last slide, and their prompts are not its content.
    if master_text and (start := body.find(master_text, body.rfind("-----"))) != -1:
        body = body[:start]

    sections = _SLIDE_SEPARATOR.split(body)[1:]
    if len(sections) != len(slide_notes):
        # A slide holding a line of dashes of its own: better one block than the
        # wrong slide numbers.
        logger.warning("Expected %d ODP slides, found %d separators", len(slide_notes), len(sections))
        sections = [_SLIDE_SEPARATOR.sub("", body)]
        slide_notes = [[note for notes in slide_notes for note in notes]]

    pictures = _Pictures(extracted.images or [], image_prefix)
    slides: list[tuple[int, str]] = []
    for slide_num, (section, notes) in enumerate(zip(sections, slide_notes, strict=True), start=1):
        md = pictures.rewrite(_TABLE_PREVIEW.sub("", _slide_markdown(section, notes)), slide_num).strip()
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
