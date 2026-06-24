"""Content-type → parser dispatch over the ``core/indexing/parsers`` stack.

``ParserDispatcher`` is the single ``DocumentParser`` the indexing pipeline
and the extract path use. It routes each ``Document`` to the right concrete
parser by ``Document.content_type`` (``DocumentType``), resolving the PDF and
audio backends from the existing ``config.loader.file_loaders`` map so behavior
matches the GPU pools ``bootstrap.py`` provisions.

This replaces the transitional ``DocSerializerBridgeParser`` (which delegated
to the legacy ``BaseLoader`` registry). Backends are built lazily on first use
and cached, so importing this module is cheap and a deploy never needs a
pool/library for a backend it doesn't exercise.
"""

from __future__ import annotations

import importlib
from typing import Any

from core.indexing.parsers.document_parser import DocumentParser
from core.models.document import Document, DocumentType, ProcessedDocument
from core.utils.logging import get_logger

logger = get_logger()

# Translate the legacy ``file_loaders`` class-name values into new registry
# backend names. Mirrors ``bootstrap.get_marker_pool`` / ``init_audio_actor`` so
# the dispatcher always resolves to a backend whose pool actually exists.
_PDF_BACKENDS: dict[str, str] = {
    "MarkerLoader": "marker",
    "DoclingLoader": "docling",
    "PyMuPDFLoader": "pymupdf",
    "DotsOCRLoader": "pdf_client",
    "OpenAILoader": "pdf_client",
}
_AUDIO_BACKENDS: dict[str, str] = {
    "LocalWhisperLoader": "local_whisper",
    "OpenAIAudioLoader": "audio_client",
}

# Attachment ext (lowercased, no dot) → DocumentType, used to wire the EML
# parser's per-attachment sub-parsers.
_MAX_EML_ATTACHMENT_DEPTH = 3
_EML_ATTACHMENT_TYPES: dict[str, DocumentType] = {
    "txt": DocumentType.TEXT,
    "md": DocumentType.MARKDOWN,
    "html": DocumentType.HTML,
    "htm": DocumentType.HTML,
    "eml": DocumentType.EML,
    "docx": DocumentType.DOCX,
    "doc": DocumentType.DOC,
    "pptx": DocumentType.PPTX,
    "pdf": DocumentType.PDF,
    "png": DocumentType.IMAGE,
    "jpg": DocumentType.IMAGE,
    "jpeg": DocumentType.IMAGE,
    "gif": DocumentType.IMAGE,
    "webp": DocumentType.IMAGE,
    "bmp": DocumentType.IMAGE,
    "svg": DocumentType.IMAGE,
}


def _create(module_path: str, name: str, **kwargs: Any) -> DocumentParser:
    """Import a parser module (triggering registration) then build it by name."""
    importlib.import_module(module_path)
    from core.indexing.parsers.registry import parser_registry

    return parser_registry.create(name, **kwargs)


class ParserDispatcher(DocumentParser):
    """Route a document to the configured concrete parser by content type."""

    def __init__(self, config: Any) -> None:
        self._config = config
        self._by_name: dict[str, DocumentParser] = {}

    def supported_types(self) -> list[str]:
        return [doc_type.value for doc_type in DocumentType]

    async def parse(self, document: Document) -> ProcessedDocument:
        backend = self._resolve_backend(document.content_type, _suffix(document.filename))
        parser = self._get(backend)
        return await parser.parse(document)

    # ----- backend resolution -----

    def _resolve_backend(self, content_type: DocumentType, ext: str) -> str:
        if content_type is DocumentType.PDF:
            return self._resolve_pdf_backend()
        if content_type in (DocumentType.AUDIO, DocumentType.VIDEO):
            return self._resolve_audio_backend(ext)
        # Every other type maps 1:1 to a registered parser whose name is the
        # type value (TEXT->"text", DOCX->"docx", EML->"eml", ...).
        return content_type.value

    def _resolve_pdf_backend(self) -> str:
        configured = self._config.loader.file_loaders.pdf
        backend = _PDF_BACKENDS.get(configured)
        if backend is None:
            raise ValueError(
                f"Unsupported PDF loader configuration {configured!r}; expected one of {sorted(_PDF_BACKENDS)}"
            )
        return backend

    def _resolve_audio_backend(self, ext: str) -> str:
        file_loaders = self._config.loader.file_loaders
        configured = (
            getattr(file_loaders, ext, None) or getattr(file_loaders, "mp3", None) or getattr(file_loaders, "wav", None)
        )
        backend = _AUDIO_BACKENDS.get(configured)
        if backend is None:
            raise ValueError(
                f"Unsupported audio loader configuration {configured!r}; expected one of {sorted(_AUDIO_BACKENDS)}"
            )
        return backend

    # ----- lazy backend construction -----

    def _get(self, name: str) -> DocumentParser:
        parser = self._by_name.get(name)
        if parser is None:
            parser = self._build(name)
            self._by_name[name] = parser
        return parser

    def _build(self, name: str) -> DocumentParser:
        logger.debug(f"Building parser backend: {name}")
        builder = _BUILDERS.get(name)
        if builder is not None:
            return builder(self)
        # Convention for simple, dependency-free parsers: registry name ``X``
        # lives in ``core.indexing.parsers.X_parser`` and registers as ``X``.
        return _create(f"core.indexing.parsers.{name}_parser", name)

    # ----- backend builders (lazy heavy imports live inside these) -----

    def _build_eml(self, attachment_depth: int = 0) -> DocumentParser:
        attachment_parsers: dict[str, DocumentParser] = {}
        for ext, dtype in _EML_ATTACHMENT_TYPES.items():
            try:
                if dtype is DocumentType.EML:
                    if attachment_depth >= _MAX_EML_ATTACHMENT_DEPTH:
                        continue
                    attachment_parsers[ext] = self._build_eml(attachment_depth + 1)
                    continue
                attachment_parsers[ext] = self._get(self._resolve_backend(dtype, ext))
            except Exception as exc:  # a missing backend must not break .eml parsing
                logger.warning(f"EML attachment parser for '.{ext}' unavailable: {exc}")
        return _create("core.indexing.parsers.eml_parser", "eml", attachment_parsers=attachment_parsers)

    def _build_marker(self) -> DocumentParser:
        from services.workers.parsers.marker_workers import MarkerLoader

        return _create("core.indexing.parsers.pdf.marker", "marker", pool=MarkerLoader())

    def _build_docling(self) -> DocumentParser:
        from services.workers.parsers.docling_workers import DoclingLoader

        return _create("core.indexing.parsers.pdf.docling", "docling", pool=DoclingLoader())

    def _build_local_whisper(self) -> DocumentParser:
        from services.workers.parsers.whisper_workers import LocalWhisperLoader

        return _create("core.indexing.parsers.audio.local_whisper", "local_whisper", pool=LocalWhisperLoader())

    def _build_pdf_client(self) -> DocumentParser:
        from services.inference.parsers.dotsocr import DotsOCRPdfClient

        ocfg = self._config.loader.openai
        vlm = _build_vlm(
            ocfg.base_url,
            ocfg.model,
            ocfg.api_key,
            ocfg.timeout,
            ocfg.enable_thinking,
        )
        client = DotsOCRPdfClient(vlm, concurrency_limit=ocfg.concurrency_limit)
        return _create("core.indexing.parsers.pdf.client_based", "pdf_client", client=client)

    def _build_audio_client(self) -> DocumentParser:
        from services.inference.parsers.openai_audio import OpenAIAudioClient

        tcfg = self._config.loader.transcriber
        language_detector = None
        if tcfg.use_whisper_lang_detector:
            from services.workers.parsers.whisper_workers import detect_language_via_actor

            async def language_detector(path):  # noqa: E731 - small adapter to the (path) -> str|None contract
                return await detect_language_via_actor(path)

        client = OpenAIAudioClient(
            base_url=tcfg.base_url,
            api_key=tcfg.api_key,
            model=tcfg.model_name,
            timeout=tcfg.timeout,
            direct_upload_suffixes=tcfg.direct_upload_suffixes,
            language_detector=language_detector,
            concurrency_limit=tcfg.max_concurrent_chunks,
        )
        return _create("core.indexing.parsers.audio.client_based", "audio_client", client=client)


# Only backends that need a non-conventional module path (``pymupdf`` lives
# under ``pdf/``) or injected dependencies (pooled/client/eml). Everything else
# is built by convention in ``ParserDispatcher._build``.
_BUILDERS: dict[str, Any] = {
    "pymupdf": lambda d: _create("core.indexing.parsers.pdf.pymupdf", "pymupdf"),
    "eml": lambda d: d._build_eml(),
    "marker": lambda d: d._build_marker(),
    "docling": lambda d: d._build_docling(),
    "local_whisper": lambda d: d._build_local_whisper(),
    "pdf_client": lambda d: d._build_pdf_client(),
    "audio_client": lambda d: d._build_audio_client(),
}


def _suffix(filename: str) -> str:
    """Lowercased extension without the dot (``"report.PDF"`` → ``"pdf"``)."""
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def _build_vlm(base_url: str, model: str, api_key: str, timeout: float, enable_thinking: bool | None = None) -> Any:
    """Construct a vLLM-backed VLM client for VLM-OCR / captioning."""
    import services.inference.vllm_client  # noqa: F401 - registers "vllm"
    from core.vlm import vlm_registry

    return vlm_registry.create(
        "vllm",
        endpoint=base_url,
        model_name=model,
        api_key=api_key,
        timeout=timeout,
        enable_thinking=enable_thinking,
    )


def build_parser_dispatcher(config: Any) -> ParserDispatcher:
    """Build the content-type dispatcher over the new parser stack."""
    return ParserDispatcher(config)


def build_caption_vlm(config: Any) -> Any | None:
    """Build the captioning VLM, or ``None`` when no VLM endpoint is configured.

    This only decides VLM *availability* (an endpoint must be set), not the
    captioning *policy*, which is applied downstream:

    - Standalone image files are always captioned when a VLM is available —
      their caption is the only text content (legacy ``ImageLoader`` parity,
      which never consulted ``image_captioning``).
    - Images embedded in other documents are gated by the global
      ``config.loader.image_captioning`` flag and the per-partition
      ``enable_image_captioning`` setting (see ``IndexingPipeline``).
    """
    vlm_cfg = config.vlm
    if not getattr(vlm_cfg, "base_url", ""):
        return None
    return _build_vlm(
        vlm_cfg.base_url,
        vlm_cfg.model,
        vlm_cfg.api_key,
        vlm_cfg.timeout,
        vlm_cfg.enable_thinking,
    )


__all__ = ["ParserDispatcher", "build_parser_dispatcher", "build_caption_vlm"]
