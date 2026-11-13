"""Text of drawn shapes: text boxes on a worksheet, and text drawn on a chart.

People annotate charts and dashboards with text boxes ("Q3 dip: supplier
strike"). That text is often the only explanation of what a chart shows, so it
is indexed with the chart it is drawn on, or with its worksheet.

Both live in DrawingML: an ``xdr:sp`` in a worksheet drawing, a ``cdr:sp`` in
a chart's own drawing (``c:userShapes``), each with an ``a:txBody`` of
paragraphs. A shape can be hidden, and its text can be linked to a cell
(``textlink="$A$1"``): the drawing then caches that cell's value, and the
chart modes treat it like any other text read from a cell.
"""

from __future__ import annotations

from dataclasses import dataclass
from xml.etree.ElementTree import Element

# A text box is a label or a note: a long text in one is cut, not copied whole.
MAX_SHAPE_TEXT = 4000


@dataclass(frozen=True)
class ShapeText:
    text: str
    # Hidden in Excel (``cNvPr hidden="1"``).
    hidden: bool = False
    # The cell the text is linked to, as written (``$A$1``, ``Notes!$B$2``).
    textlink: str | None = None


def shape_text(shape: Element) -> ShapeText | None:
    """Read one ``sp`` element; None when it holds no text."""
    lines = []
    for paragraph in shape.iterfind("{*}txBody/{*}p"):
        line = " ".join("".join(run.text or "" for run in paragraph.iter() if _local(run) == "t").split())
        if line:
            lines.append(line)
    text = "\n".join(lines)[:MAX_SHAPE_TEXT]
    if not text:
        return None
    properties = shape.find("{*}nvSpPr/{*}cNvPr")
    hidden = properties is not None and properties.get("hidden", "0").lower() in {"1", "true"}
    return ShapeText(text, hidden, (shape.get("textlink") or "").strip() or None)


def drawn_texts(root: Element) -> tuple[ShapeText, ...]:
    """Every shape with text under ``root``, groups included, in drawing order."""
    return tuple(found for shape in root.iterfind(".//{*}sp") if (found := shape_text(shape)) is not None)


def _local(element: Element) -> str:
    return element.tag.rsplit("}", 1)[-1] if isinstance(element.tag, str) else ""
