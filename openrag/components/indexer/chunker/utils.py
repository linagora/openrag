import re
from typing import Literal, Optional

# Regex to match a Markdown table (header + delimiter + at least one row)
TABLE_RE = re.compile(
    r"((?:^|\n)\|.*?\|\r?\n\|\s*[:-]+(?:\s*\|[:-]+)*\|\r?\n(?:\|.*?\|\r?\n)+)",
    re.DOTALL | re.MULTILINE,
)

# Regex to match image descriptions
IMAGE_RE = re.compile(r"(<image_description>(.*?)</image_description>)", re.DOTALL)

# Regex to match page markers
PAGE_RE = re.compile(r"\[PAGE_(\d+)\]")


class MDElement:
    """Class representing a segment of markdown content."""

    def __init__(
        self,
        type: Literal["text", "table", "image"],
        content: str,
        page_number: Optional[int] = None,
    ):
        self.type = type  # 'text', 'table', 'image'
        self.content = content
        self.page_number = page_number

    def __repr__(self):
        return f"Element(type={self.type}, page_number={self.page_number}, content={self.content[:100]}...)"


def span_inside(span: tuple[int, int], container: tuple[int, int]) -> bool:
    return container[0] <= span[0] and span[1] <= container[1]


def get_page_number(position, page_markers):
    """
    Given a position in the text and list of (position, page_number) tuples,
    return the page number for that position.
    Content after [PAGE_N] marker belongs to page N+1.
    """
    current_page = 1  # Default to page 1 if before any markers
    for marker_pos, page_num in page_markers:
        if position >= marker_pos:
            current_page = page_num + 1  # Content after [PAGE_N] is on page N+1
        else:
            break
    return current_page


def split_md_elements(md_text: str) -> list[MDElement]:
    """
    Split markdown text into segments of text, tables, and images.
    Returns a list of tuples:
    - ('text', content) for text segments
    - ('table', content, page_number) for tables
    - ('image', content, page_number) for images
    """
    # Find all page markers
    page_markers = []
    for match in PAGE_RE.finditer(md_text):
        page_markers.append((match.start(), int(match.group(1))))
    page_markers.sort()  # Ensure they're in order

    all_matches = []

    # Find image matches first and record their spans
    image_spans = []
    for match in IMAGE_RE.finditer(md_text):
        span = match.span()
        page_num = get_page_number(span[0], page_markers)
        all_matches.append((span, "image", match.group(1).strip(), page_num))
        image_spans.append(span)

    # Find table matches, but skip those that are fully inside an image description
    for match in TABLE_RE.finditer(md_text):
        span = match.span()
        if not any(span_inside(span, image_span) for image_span in image_spans):
            page_num = get_page_number(span[0], page_markers)
            all_matches.append((span, "table", match.group(1).strip(), page_num))

    # Sort matches by start position
    all_matches.sort(key=lambda x: x[0][0])

    parts = []
    last = 0

    for (start, end), match_type, content, page_num in all_matches:
        # Add text segment before this match if there is any
        if start > last:
            text_segment = md_text[last:start]
            if text_segment.strip():  # Only add non-empty text segments
                parts.append(("text", text_segment.strip()))

        # Add the matched segment with page number
        parts.append((match_type, content, page_num))
        last = end

    # Add remaining text after the last match
    if last < len(md_text):
        remaining_text = md_text[last:]
        if remaining_text.strip():  # Only add non-empty text segments
            parts.append(("text", remaining_text.strip()))

    return [MDElement(*p) for p in parts]


def get_chunk_page_number(chunk_str: str, previous_chunk_ending_page=1):
    """
    Determine the start and end pages for a text chunk containing [PAGE_N] separators.
    PAGE_N marks the end of page N - text before separator is on page N.
    """
    # Find all page separator matches in the chunk
    matches = list(PAGE_RE.finditer(chunk_str))

    if not matches:
        # No separators found - entire chunk is on previous page
        return {
            "start_page": previous_chunk_ending_page,
            "end_page": previous_chunk_ending_page,
        }

    first_match = matches[0]
    last_match = matches[-1]
    last_char_idx = len(chunk_str) - 1

    # Determine start page
    if first_match.start() == 0:
        # Chunk starts with a separator - begins on next page
        start_page = int(first_match.group(1)) + 1
    else:
        # Text precedes first separator - starts on previous page
        start_page = previous_chunk_ending_page

    # Determine end page
    if last_match.end() - 1 == last_char_idx:
        # Chunk ends exactly at a separator - ends on that page
        end_page = int(last_match.group(1))
    else:
        # Chunk ends after separator - ends on next page
        end_page = int(last_match.group(1)) + 1

    return {"start_page": start_page, "end_page": end_page}


def parse_markdown_table(markdown_table):
    """
    Parse a markdown table and extract header and groups based on Domain column.

    Returns:
        tuple: (header_lines, groups)
            - header_lines: list of [header_row, separator_row]
            - groups: list of lists, each containing rows belonging to one domain
    """
    lines = markdown_table.strip().split("\n")

    # Extract header (first 2 lines)
    header_lines = lines[:2]
    data_rows = lines[2:]

    # Group rows by Domain (first column)
    groups = []
    current_group = []

    for row in data_rows:
        # Parse first column (Domain)
        cells = [cell.strip() for cell in row.split("|")[1:-1]]
        if not cells:
            continue  # skip malformed rows

        domain = cells[0]

        # If Domain is not empty, start a new group
        if domain:
            if current_group:  # Save previous group
                groups.append(current_group)
            current_group = [row]  # Start new group
        else:
            # Domain is empty, continue current group
            current_group.append(row)

    # Don't forget the last group
    if current_group:
        groups.append(current_group)

    return header_lines, groups


def chunk_table(
    table_element: MDElement,
    chunk_size: int = 512,
    length_function: Optional[callable] = None,
) -> list[MDElement]:
    txt = clean_markdown_table_spacing(table_element.content)
    header_lines, groups = parse_markdown_table(txt)

    # Convert header lines → text block
    header_text = "\n".join(header_lines)

    # Convert group lists → text blocks
    group_texts = ["\n".join(g) for g in groups]

    # Precompute token length
    header_ntoks = length_function(header_text)
    groups_ntoks = [length_function(g) for g in group_texts]

    subtables = []
    current_rows = [header_text]
    current_size = header_ntoks

    prev_last_row = None  # for overlap

    for group_txt, g_ntoks in zip(group_texts, groups_ntoks, strict=True):
        # If adding this group exceeds the chunk limit
        if current_size + g_ntoks > chunk_size:
            # ---- finalize current subtable ----
            subtables.append("\n".join(current_rows))

            # ---- start new subtable with OVERLAP ----
            current_rows = [header_text]  # always restart headers
            if prev_last_row:
                current_rows.append(prev_last_row)  # add overlapping row

            current_rows.append(group_txt)
            current_size = (
                header_ntoks
                + (length_function(prev_last_row) if prev_last_row else 0)
                + g_ntoks
            )

        else:
            # fits → just append normally
            current_rows.append(group_txt)
            current_size += g_ntoks

        # track last row for overlap
        prev_last_row = group_txt

    # finalize last subtable
    if current_rows:
        subtables.append("\n".join(current_rows))

    # wrap into MDElement list
    return [
        MDElement(type="table", content=subtable, page_number=table_element.page_number)
        for subtable in subtables
    ]


def clean_markdown_table_spacing(markdown_table: str) -> str:
    """
    Normalize spacing inside a markdown table:
    - trims each cell
    - keeps table shape intact
    """

    cleaned_lines = []

    for line in markdown_table.strip().split("\n"):
        if "|" not in line:
            cleaned_lines.append(line.strip())
            continue

        # Split row into cells (preserve leading/trailing pipes)
        parts = line.split("|")

        # Strip each cell except the outer empty ones
        cleaned_cells = [cell.strip() for cell in parts]

        # Rebuild with a single space around each cell
        new_line = "| " + " | ".join(cleaned_cells[1:-1]) + " |"
        cleaned_lines.append(new_line)

    return "\n".join(cleaned_lines)
