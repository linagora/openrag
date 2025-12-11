import re
from typing import Literal

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
        page_number: int = None,
    ):
        """
        Initialize an MDElement representing a segment of markdown content.
        
        Parameters:
            type (Literal["text", "table", "image"]): The element kind: "text" for plain markdown, "table" for markdown tables, and "image" for image blocks.
            content (str): Raw markdown content for this element.
            page_number (int | None): Optional 1-based page number associated with the element; use None when no page information is available.
        """
        self.type = type  # 'text', 'table', 'image'
        self.content = content
        self.page_number = page_number

    def __repr__(self):
        """
        Provide a concise representation of the element for debugging that includes its type, page number, and a truncated content preview.
        
        Returns:
            str: A string containing the element's type, page number, and up to the first 100 characters of content followed by an ellipsis.
        """
        return f"Element(type={self.type}, page_number={self.page_number}, content={self.content[:100]}...)"


def span_inside(span: tuple[int, int], container: tuple[int, int]) -> bool:
    """
    Determine whether a span lies entirely within a containing span.
    
    Parameters:
        span (tuple[int, int]): (start, end) indices of the inner span.
        container (tuple[int, int]): (start, end) indices of the container span.
    
    Returns:
        True if the inner span's start is greater than or equal to the container's start and its end is less than or equal to the container's end, False otherwise.
    """
    return container[0] <= span[0] and span[1] <= container[1]


def get_page_number(position, page_markers):
    """
    Determine the page number for a given character position using page markers.
    
    Parameters:
        position (int): Character index in the text whose page number to determine.
        page_markers (list[tuple[int, int]]): Ordered list of (marker_position, marker_page) tuples.
            Each tuple represents a [PAGE_N] marker found at marker_position with N == marker_page.
            Markers must be in ascending marker_position order.
    
    Returns:
        int: The page number containing the position. Positions at or after a marker at page N
        are considered to be on page N+1. Returns 1 if the position is before all markers.
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
    Split markdown into ordered MDElement segments representing text, tables, and images.
    
    The function scans md_text for markdown tables, image_description tags, and page separators of the form `[PAGE_N]`, then produces a list of MDElement instances preserving the original document order. Table and image elements receive a page_number determined from the nearest preceding `[PAGE_N]` marker; text segments are included for content between matches (text elements may have no page_number). Tables that are entirely contained inside an image description are treated as part of that image and not emitted separately.
    
    Parameters:
        md_text (str): The full markdown content to split.
    
    Returns:
        list[MDElement]: Ordered list of MDElement objects with types "text", "table", or "image". Table and image elements include their associated page_number; text elements contain the intervening content.
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
    Compute the starting and ending page numbers for a text chunk that may contain `[PAGE_N]` separators.
    
    Parameters:
        chunk_str (str): The text chunk which may include one or more `[PAGE_N]` markers.
        previous_chunk_ending_page (int): The page number where the previous chunk ended; used when the chunk begins before its first marker.
    
    Returns:
        dict: A mapping with keys:
            - "start_page": int — the page number where the chunk's content begins.
            - "end_page": int — the page number where the chunk's content ends.
    
    Behavior:
        - If the chunk contains no `[PAGE_N]` markers, both start and end pages equal `previous_chunk_ending_page`.
        - If the chunk starts with `[PAGE_N]`, the chunk begins on page `N+1`; otherwise it begins on `previous_chunk_ending_page`.
        - If the chunk ends exactly at a `[PAGE_N]` marker, the chunk ends on page `N`; otherwise it ends on page `N+1`.
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
    Extract the table header and group data rows by the first column (Domain).
    
    Parameters:
        markdown_table (str): A Markdown table string including header, delimiter, and data rows.
    
    Returns:
        tuple: (header_lines, groups)
            header_lines (list[str]): The first two lines of the table (header row and delimiter row).
            groups (list[list[str]]): Lists of data-row strings grouped so that each group starts with a row whose first column (Domain) is non-empty and includes subsequent rows whose first column is empty.
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
    table_element: MDElement, chunk_size: int = 512, length_function: callable = None
) -> list[MDElement]:
    """
    Split a markdown table MDElement into smaller table MDElement chunks that respect a token-length limit.
    
    The resulting subtables always include the original table header; when a split is necessary, the last data group of the previous chunk is repeated as an overlap at the start of the next chunk to preserve continuity.
    
    Parameters:
        table_element (MDElement): The source table element whose `content` is a markdown table and whose `page_number` will be preserved on output.
        chunk_size (int): Maximum allowed token length for each subtable chunk.
        length_function (callable): Function that accepts a string and returns its token length (used to measure header and group sizes).
    
    Returns:
        list[MDElement]: A list of new MDElement objects of type "table", each containing a subtable string and the same page_number as the input.
    """
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

    for group_txt, g_ntoks in zip(group_texts, groups_ntoks):
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
    Normalize spacing in a Markdown table while preserving its structure.
    
    Trim whitespace from each cell and ensure a single space surrounds cell contents and pipe characters. Lines without a pipe character are trimmed and preserved as-is. The function preserves the number and order of cells and the presence of leading/trailing pipes.
    
    Parameters:
        markdown_table (str): The markdown table text to normalize.
    
    Returns:
        str: The markdown table with normalized spacing.
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