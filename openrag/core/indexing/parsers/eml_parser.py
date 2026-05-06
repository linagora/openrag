"""EML (RFC822 email) ``DocumentParser`` implementation.

Extracts the message body (``text/plain`` preferred, ``text/html``
fallback) and dispatches each attachment to a parser supplied via DI.
Email headers (subject, from, to, date, message-id) and an attachment
manifest are merged into the output ``ProcessedDocument.metadata``.

Attachment dispatch contract:

- ``attachment_parsers`` maps lowercased extension (``"pdf"``, ``"docx"``,
  no leading dot) to a :class:`DocumentParser`.
- Each attachment becomes a synthetic :class:`Document` (raw bytes, the
  appropriate ``DocumentType`` if recognised, ``DocumentType.TEXT`` otherwise).
- The dispatched parser's text output is appended after a header block
  giving filename, content-type, and size.
- Any ``ImageBlock``s emitted by the dispatched parser are propagated
  into the EML's own ``ProcessedDocument.images``.
- Image attachments with no registered parser are emitted directly as
  ``ImageBlock``s (no ``markdown_ref`` — there is no in-body placeholder
  for them; see :class:`ImageBlock` for the parser→caption contract).
- Unknown non-image attachments include only the manifest header.

Failures are tolerated: a single attachment that errors does not
propagate; we log and continue.
"""

from __future__ import annotations

import email
import logging
from collections.abc import Mapping
from email.utils import parsedate_to_datetime

from ...models.document import Document, DocumentType, ImageBlock, ProcessedDocument, TextBlock
from .document_parser import DocumentParser
from .registry import parser_registry

logger = logging.getLogger(__name__)


_IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "svg"}


@parser_registry.register("eml")
class EmlParser(DocumentParser):
    """Parse ``.eml`` into a single text block plus ImageBlocks; dispatch attachments via DI."""

    def __init__(self, attachment_parsers: Mapping[str, DocumentParser] | None = None) -> None:
        self._attachment_parsers = dict(attachment_parsers or {})

    def supported_types(self) -> list[str]:
        return [DocumentType.EML.value]

    async def parse(self, document: Document) -> ProcessedDocument:
        if not document.raw_bytes:
            return ProcessedDocument(
                document_id=document.id,
                metadata=dict(document.metadata),
            )

        try:
            msg = email.message_from_bytes(document.raw_bytes)
        except Exception as exc:
            logger.warning("Failed to parse EML: %s", exc)
            return ProcessedDocument(
                document_id=document.id,
                metadata=dict(document.metadata),
            )

        headers = self._extract_headers(msg)
        body, attachments = self._walk_parts(msg)

        attachments_text, images = await self._render_attachments(attachments)
        full_text = (body + attachments_text).strip()

        metadata = dict(document.metadata)
        metadata.update(
            {
                "email_subject": headers["subject"],
                "email_from": headers["from"],
                "email_to": headers["to"],
                "email_date": headers["date"],
                "email_message_id": headers["message-id"],
                "email_attachment_count": len(attachments),
                "email_attachment_filenames": [a["filename"] for a in attachments],
            }
        )
        if attachments:
            metadata["email_attachments"] = [
                {"filename": a["filename"], "content_type": a["content_type"], "size": a["size"]} for a in attachments
            ]

        text_blocks = [TextBlock(text=full_text, page_number=1)] if full_text else []
        return ProcessedDocument(
            document_id=document.id,
            text_blocks=text_blocks,
            images=images,
            metadata=metadata,
            page_count=1 if full_text else 0,
        )

    # ----- helpers -----

    @staticmethod
    def _extract_headers(msg: email.message.Message) -> dict[str, str]:
        headers = {
            "subject": msg.get("subject", "") or "",
            "from": msg.get("from", "") or "",
            "to": msg.get("to", "") or "",
            "date": msg.get("date", "") or "",
            "message-id": msg.get("message-id", "") or "",
        }
        if headers["date"]:
            try:
                headers["date"] = parsedate_to_datetime(headers["date"]).isoformat()
            except Exception:
                pass
        return headers

    @staticmethod
    def _walk_parts(msg: email.message.Message) -> tuple[str, list[dict]]:
        body = ""
        attachments: list[dict] = []

        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = part.get_content_disposition()

            if disposition in ("attachment", "inline"):
                filename = part.get_filename()
                payload = part.get_payload(decode=True)
                if filename and payload:
                    attachments.append(
                        {
                            "filename": filename,
                            "content_type": content_type,
                            "size": len(payload),
                            "raw": payload,
                        }
                    )
                continue

            if content_type in ("text/plain", "text/html"):
                payload = part.get_payload(decode=True)
                if not payload:
                    continue
                try:
                    text = payload.decode("utf-8") if isinstance(payload, bytes) else str(payload)
                except UnicodeDecodeError:
                    text = payload.decode("latin-1", errors="ignore") if isinstance(payload, bytes) else str(payload)
                # text/plain wins; only use text/html if we have nothing yet
                if content_type == "text/plain" or not body:
                    body = text

        return body.strip(), attachments

    async def _render_attachments(self, attachments: list[dict]) -> tuple[str, list[ImageBlock]]:
        """Render the attachment-section text and collect any ImageBlocks.

        Returns ``("", [])`` when there are no attachments.
        """
        if not attachments:
            return "", []

        rendered: list[str] = ["\n\n--- ATTACHMENTS ---\n"]
        images: list[ImageBlock] = []
        for att in attachments:
            ext = self._extension(att["filename"])
            header = (
                f"\nAttachment: {att['filename']}\nContent-Type: {att['content_type']}\nSize: {att['size']} bytes\n"
            )
            content, att_images = await self._render_one(att, ext)
            rendered.append(header + content + "---\n")
            images.extend(att_images)
        return "".join(rendered), images

    async def _render_one(self, attachment: dict, ext: str) -> tuple[str, list[ImageBlock]]:
        """Dispatch one attachment. Returns ``(text_to_inline, image_blocks)``."""
        parser = self._attachment_parsers.get(ext)
        if parser is not None:
            try:
                doc = Document(
                    filename=attachment["filename"],
                    raw_bytes=attachment["raw"],
                    content_type=Document.detect_content_type(attachment["filename"]),
                    metadata={"source": f"attachment:{attachment['filename']}"},
                )
                processed = await parser.parse(doc)
                content = "\n\n".join(b.text for b in processed.text_blocks if b.text)
                inline = f"Content:\n{content}\n" if content else ""
                return inline, list(processed.images)
            except Exception as exc:
                logger.warning("Attachment parser failed for %s: %s", attachment["filename"], exc)

        if ext in _IMAGE_EXTS:
            # No parser registered — emit the image as an ImageBlock so a
            # downstream caption stage can describe it. No ``markdown_ref``
            # because there is no in-body placeholder pointing to it.
            return "", [
                ImageBlock(
                    image_bytes=attachment["raw"],
                    page_number=1,
                    mime_type=attachment["content_type"] or "image/png",
                    metadata={"source": f"attachment:{attachment['filename']}"},
                )
            ]

        return "", []

    @staticmethod
    def _extension(filename: str) -> str:
        return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
