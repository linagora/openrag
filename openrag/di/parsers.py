"""Register document parser implementations with the core registry."""


def register_parsers() -> None:
    """Import parser implementations so they register with the core registry."""
    import core.indexing.parsers.audio.client_based  # noqa: F401
    import core.indexing.parsers.audio.local_whisper  # noqa: F401
    import core.indexing.parsers.doc_parser  # noqa: F401
    import core.indexing.parsers.docx_parser  # noqa: F401
    import core.indexing.parsers.eml_parser  # noqa: F401
    import core.indexing.parsers.html_parser  # noqa: F401
    import core.indexing.parsers.image_parser  # noqa: F401
    import core.indexing.parsers.markdown_parser  # noqa: F401
    import core.indexing.parsers.pdf.client_based  # noqa: F401
    import core.indexing.parsers.pdf.docling  # noqa: F401
    import core.indexing.parsers.pdf.marker  # noqa: F401
    import core.indexing.parsers.pdf.marker_serve  # noqa: F401
    import core.indexing.parsers.pdf.pymupdf  # noqa: F401
    import core.indexing.parsers.pptx_parser  # noqa: F401
    import core.indexing.parsers.text_parser  # noqa: F401
