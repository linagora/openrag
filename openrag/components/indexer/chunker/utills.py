from langchain_core.documents.base import Document
from langchain_openai import ChatOpenAI
import re


# Regex to match a Markdown table (header + delimiter + at least one row)
TABLE_RE = re.compile(
    r"(\n\|.*?\|\r?\n\|\s*[:-]+(?:\s*\|[:-]+)*\|\r?\n(?:\|.*?\|\r?\n)+)", re.DOTALL
)

# Regex to match image descriptions
IMAGE_RE = re.compile(r"(<image_description>(.*?)</image_description>)", re.DOTALL)


def _get_token_length(documents: str | Document, llm: ChatOpenAI = None) -> int:
    """Calculate the total number of tokens in a list of documents."""
    length_func = llm.get_num_tokens if llm else lambda x: len(x)
    num_tokens = []
    for doc in documents:
        if isinstance(doc, str):
            num_tokens.append(length_func(doc))
        elif isinstance(doc, Document):
            num_tokens.append(length_func(doc.page_content))
        else:
            raise ValueError("Documents must be either str or Document instances.")
    return sum(num_tokens)


def combine_chunks(
    chunks: list[str | Document | tuple[str, str]],
    llm: ChatOpenAI = None,
    chunk_max_size: int = 512,
) -> list[str]:  # type: ignore
    doc_n_tokens = map(lambda doc: _get_token_length(documents=[doc], llm=llm), chunks)

    # regroup subsequent chunks based if the total length is less than token_max
    grouped_docs = []
    current_group = []
    current_length = 0

    for doc, n_tokens in zip(chunks, doc_n_tokens):
        if isinstance(doc, Document):
            doc = doc.page_content

        if current_length + n_tokens > chunk_max_size:
            if current_group:
                grouped_docs.append(current_group)
            current_group = [doc]
            current_length = n_tokens
        else:
            current_group.append(doc)
            current_length += n_tokens

    if current_group:
        grouped_docs.append(current_group)

    return ["\n".join(group) for group in grouped_docs]


def span_inside(span, container):
    return container[0] <= span[0] and span[1] <= container[1]


def split_md_elements(md_text: str):
    """
    Split markdown text into segments of text, tables, and images.
    Returns a list of tuples: (type, content) where type is 'text', 'table', or 'image'
    """
    all_matches = []

    # Find image matches first and record their spans
    image_spans = []
    for match in IMAGE_RE.finditer(md_text):
        span = match.span()
        all_matches.append((span, "image", match.group(1)))
        image_spans.append(span)

    # Find table matches, but skip those that are fully inside an image description
    for match in TABLE_RE.finditer(md_text):
        span = match.span()
        if not any(span_inside(span, image_span) for image_span in image_spans):
            all_matches.append((span, "table", match.group(1)))

    # Sort matches by start position
    all_matches.sort(key=lambda x: x[0][0])

    parts = []
    last = 0

    for (start, end), match_type, content in all_matches:
        # Add text segment before this match if there is any
        if start > last:
            text_segment = md_text[last:start]
            if text_segment.strip():  # Only add non-empty text segments
                parts.append(("text", text_segment.strip()))

        # Add the matched segment
        parts.append((match_type, content.strip()))
        last = end

    # Add remaining text after the last match
    if last < len(md_text):
        remaining_text = md_text[last:]
        if remaining_text.strip():  # Only add non-empty text segments
            parts.append(("text", remaining_text.strip()))

    return parts


def add_overlap(
    chunks: list[tuple[str, str]],
    target_chunk_types: list[str],
    add_before: bool = True,
    add_after: bool = False,
    chunk_overlap: float = None,
) -> list[tuple[str, str]]:
    """
    Add overlap from adjacent text chunks to specified chunk types.

    Args:
        chunks: List of (chunk_type, chunk_content) tuples
        target_chunk_types: List of chunk types to add overlap to (e.g., ['table', 'image'])
        add_before: Whether to add overlap from previous text chunk
        add_after: Whether to add overlap from next text chunk
        chunk_overlap: Overlap ratio (uses self.chunk_overlap if None)

    Returns:
        List of (chunk_type, chunk_content) tuples with overlap added
    """
    overlap_chars = int(chunk_overlap * 4)  # Assuming 4 characters per token
    chunk_l = []

    for i, (chunk_type, chunk_content) in enumerate(chunks):
        modified_chunk = chunk_content

        if chunk_type in target_chunk_types:
            overlap_parts = []

            # Add overlap from previous text chunk
            if add_before and i > 0:
                prev_chunk_type, prev_chunk = chunks[i - 1]
                if prev_chunk_type == "text":
                    prev_overlap = (
                        prev_chunk[-overlap_chars:]
                        if len(prev_chunk) > overlap_chars
                        else prev_chunk
                    )
                    overlap_parts.append(prev_overlap)

            # Add the original chunk
            overlap_parts.append(modified_chunk)

            # Add overlap from next text chunk
            if add_after and i < len(chunks) - 1:
                next_chunk_type, next_chunk = chunks[i + 1]
                if next_chunk_type == "text":
                    next_overlap = (
                        next_chunk[:overlap_chars]
                        if len(next_chunk) > overlap_chars
                        else next_chunk
                    )
                    overlap_parts.append(next_overlap)

            # Join all parts with newlines
            modified_chunk = "\n".join(overlap_parts)

        chunk_l.append((chunk_type, modified_chunk))

    return chunk_l
