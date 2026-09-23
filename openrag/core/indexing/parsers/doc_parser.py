"""Legacy ``.doc`` (binary Word 97-2003) ``DocumentParser``.

Converts ``.doc`` to ``.docx`` via the ``spire.doc`` library, then
delegates to :class:`DocxParser` for Markdown extraction. Falls back to
plain-text extraction (``Document.GetText()``) if Spire's conversion
fails.

Spire.Doc requires DOTNET; the constructor sets the invariant-globalization
env var that makes Spire usable without the full ICU data.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import threading
from contextlib import suppress
from pathlib import Path

from ...models.document import Document, DocumentType, ProcessedDocument, TextBlock
from .document_parser import DocumentParser
from .docx_parser import DocxParser
from .registry import parser_registry

logger = logging.getLogger(__name__)

os.environ.setdefault("DOTNET_SYSTEM_GLOBALIZATION_INVARIANT", "1")


@parser_registry.register("doc")
class DocParser(DocumentParser):
    """Parse legacy ``.doc`` files via .docx conversion + DocxParser."""

    def __init__(self, docx_parser: DocxParser | None = None) -> None:
        """Pass an explicit ``DocxParser`` to share VLM / semaphore config;
        otherwise a captioning-disabled instance is constructed.
        """
        self._docx = docx_parser or DocxParser()

    def supported_types(self) -> list[str]:
        return [DocumentType.DOC.value]

    async def parse(self, document: Document) -> ProcessedDocument:
        if not document.raw_bytes:
            return ProcessedDocument(
                document_id=document.id,
                metadata=dict(document.metadata),
            )

        # ``asyncio.to_thread`` cannot be interrupted, so on cancellation — a parse
        # timeout (``asyncio.wait_for`` in ``services/workers/stages/parse.py``) or
        # ``ray.cancel`` — this coroutine unwinds while Spire is still converting,
        # and a cleanup here would run before the file exists. ``_convert`` re-checks
        # this flag after writing and removes the file itself when nobody is left to
        # receive it. The ``finally`` is the single cleanup site for every other
        # exit, including one taken after ``docx_path`` was assigned (#846).
        abandoned = threading.Event()
        # ``asyncio.to_thread`` does not deliver the worker's result atomically:
        # ``_convert`` can find ``abandoned`` clear and hand the path over, and
        # cancellation can still reach this task before the assignment below
        # lands — leaving a file nobody holds a reference to. The worker records
        # the path here instead, under a lock it shares with the ``finally``, so
        # whichever side runs second sees what the other did.
        handoff: dict[str, str] = {}
        handoff_lock = threading.Lock()
        docx_path: str | None = None
        try:
            async with document.as_temporary_file() as src_path:
                docx_path, fallback_text = await asyncio.to_thread(
                    self._convert, str(src_path), abandoned, handoff, handoff_lock
                )

            if docx_path:
                # ``source_path`` is the *converted* file, never the inherited one:
                # ``model_copy`` propagates every field, so leaving the original
                # would hand ``DocxParser`` the .doc and it would parse the wrong
                # file. ``raw_bytes`` is dropped for the same reason it is not read
                # — the .docx stays on disk and the DOCX parser opens it (#846).
                #
                # ``filename`` moves to .docx with the content: ``as_temporary_file``
                # only yields a ``source_path`` whose suffix matches the filename's —
                # the sync libraries dispatch on it — so leaving "legacy.doc" here
                # makes it reject the converted file, fall through to ``raw_bytes``
                # (now None) and raise.
                docx_doc = document.model_copy(
                    update={
                        "raw_bytes": None,
                        "content_type": DocumentType.DOCX,
                        "source_path": docx_path,
                        "filename": f"{Path(document.filename).stem}.docx",
                    }
                )
                return await self._docx.parse(docx_doc)

            text = (fallback_text or "").strip()
            text_blocks = [TextBlock(text=text, page_number=1)] if text else []
            return ProcessedDocument(
                document_id=document.id,
                text_blocks=text_blocks,
                metadata=dict(document.metadata),
                page_count=1 if text else 0,
            )
        finally:
            # Under the lock so it cannot interleave with the hand-off: either
            # the worker recorded the path and this sees it, or this sets the
            # flag first and the worker cleans up itself.
            with handoff_lock:
                abandoned.set()
                orphan = handoff.get("path")
            if orphan:
                # Ownership moved here with the path; ``as_temporary_file``
                # deliberately does not unlink a ``source_path``, since that
                # normally belongs to the uploader rather than to us.
                with suppress(OSError):
                    os.remove(orphan)

    @staticmethod
    def _convert(
        path: str,
        abandoned: threading.Event,
        handoff: dict[str, str],
        # Untyped deliberately: ``threading.Lock`` is only a class from 3.13, and
        # this project supports 3.12, where it is still a factory function and
        # the annotation would be wrong.
        handoff_lock,
    ) -> tuple[str | None, str | None]:
        """Run blocking Spire.Doc conversion. Returns ``(docx_path, fallback_text)``.

        Exactly one of the two will be non-None on success; both ``None``
        means total failure (caller emits an empty ProcessedDocument).

        Returns the converted file's **path**, not its bytes: reading it here
        put the whole .docx in memory on top of the .doc already there (#846).
        Ownership moves with it — the caller deletes it once the DOCX parser is
        done, which is why the ``finally`` below no longer does.

        ``abandoned`` and ``handoff`` are the hand-off's other half: this runs in a thread the event
        loop cannot interrupt, so a cancelled caller is gone before the file exists
        and can neither receive the path nor remove it. Re-checking after the write
        leaves the cleanup with whichever side is still there.
        """
        try:
            from spire.doc import Document as SpireDocument
            from spire.doc import FileFormat
        except ImportError:
            logger.warning("spire.doc not available; cannot parse legacy .doc files")
            return None, None

        spire_doc = SpireDocument()
        out_path: str | None = None
        try:
            spire_doc.LoadFromFile(path)
            with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as out:
                out_path = out.name
            spire_doc.SaveToFile(out_path, FileFormat.Docx2016)
            with handoff_lock:
                if abandoned.is_set():
                    # Nobody left to receive it; leaving ``out_path`` set has the
                    # ``finally`` below remove it.
                    return None, None
                # Recorded before returning, so the caller can find the file even
                # if it never receives this result.
                handoff["path"] = out_path
                converted, out_path = out_path, None  # the caller's now, not ours
            return converted, None
        except Exception as exc:
            logger.warning("Spire.Doc .doc → .docx conversion failed (%s); falling back to plain text", exc)
            try:
                return None, spire_doc.GetText()
            except Exception as fallback_exc:
                logger.warning("Spire.Doc fallback text extraction also failed: %s", fallback_exc)
                return None, None
        finally:
            try:
                spire_doc.Close()
            except Exception:
                pass
            if out_path and os.path.exists(out_path):
                try:
                    os.remove(out_path)
                except OSError:
                    pass
