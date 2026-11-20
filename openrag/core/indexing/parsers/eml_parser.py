"""EML (RFC822 email) ``DocumentParser`` implementation.

Extracts the message body (``text/plain`` preferred, ``text/html``
fallback) and dispatches each attachment to a parser supplied via DI.
HTML-only bodies use the HTML parser so embedded images are extracted
and their base64 payloads do not reach chunks.
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

import asyncio
import email
import io
import logging
from collections.abc import Mapping
from contextlib import closing
from email import policy
from email.utils import parsedate_to_datetime

from ...models.document import Document, DocumentType, ImageBlock, ProcessedDocument, TextBlock
from ..validators import CONTENT_SNIFF_BYTES, validate_content_matches_extension, validate_ooxml_package
from .document_parser import DocumentParser
from .html_parser import HtmlParser
from .registry import parser_registry

logger = logging.getLogger(__name__)


_IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "svg"}

# Parser-bomb cap: bound the attachments processed per email so a small message
# with thousands of attachments can't exhaust CPU/memory during ingestion. (The
# nested .eml-in-.eml recursion is bounded separately at parser-tree build time
# via _MAX_EML_ATTACHMENT_DEPTH in the parser_dispatcher.)
_MAX_EML_ATTACHMENTS = 50
# Total text from tabular attachments in one email, truncation marker included.
_MAX_EML_ATTACHMENT_TEXT_BYTES = 8 * 1024 * 1024
_ATTACHMENT_TRUNCATED_MARKER = "[attachment content truncated at the safe text limit]"


class _AttachmentTextBudget:
    """Bytes of tabular attachment text one email may still emit."""

    def __init__(self, remaining: int) -> None:
        self.remaining = remaining


@parser_registry.register("eml")
class EmlParser(DocumentParser):
    """Parse ``.eml`` into a single text block plus ImageBlocks; dispatch attachments via DI."""

    def __init__(self, attachment_parsers: Mapping[str, DocumentParser] | None = None) -> None:
        self._attachment_parsers = dict(attachment_parsers or {})
        self._body_html_parser = HtmlParser()

    def supported_types(self) -> list[str]:
        return [DocumentType.EML.value]

    async def parse(self, document: Document) -> ProcessedDocument:
        if not document.raw_bytes:
            return ProcessedDocument(
                document_id=document.id,
                metadata=dict(document.metadata),
            )

        try:
            msg = email.message_from_bytes(document.raw_bytes, policy=policy.default)
        except Exception as exc:
            logger.warning("Failed to parse EML: %s", exc)
            return ProcessedDocument(
                document_id=document.id,
                metadata=dict(document.metadata),
            )

        headers = self._extract_headers(msg)
        body, body_content_type, attachments = self._walk_parts_with_type(msg)

        body_text, body_images = await self._render_body(document, body, body_content_type)
        attachments_text, attachment_images = await self._render_attachments(attachments)
        full_text = (body_text + attachments_text).strip()
        images = [*body_images, *attachment_images]

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
        # Under policy.default, msg.get(...) returns Header-like objects whose
        # str() is the RFC 2047-decoded value; cast eagerly so the metadata is
        # plain strings.
        headers = {
            "subject": str(msg.get("subject", "") or ""),
            "from": str(msg.get("from", "") or ""),
            "to": str(msg.get("to", "") or ""),
            "date": str(msg.get("date", "") or ""),
            "message-id": str(msg.get("message-id", "") or ""),
        }
        if headers["date"]:
            try:
                headers["date"] = parsedate_to_datetime(headers["date"]).isoformat()
            except Exception:
                pass
        return headers

    @staticmethod
    def _walk_parts(msg: email.message.Message) -> tuple[str, list[dict]]:
        body, _, attachments = EmlParser._walk_parts_with_type(msg)
        return body, attachments

    @staticmethod
    def _walk_parts_with_type(msg: email.message.Message) -> tuple[str, str, list[dict]]:
        body = ""
        body_content_type = ""
        attachments: list[dict] = []
        attachment_candidates = 0

        for part in msg.walk():
            content_type = part.get_content_type()
            try:
                disposition = part.get_content_disposition()
            except ValueError as exc:
                logger.warning("Skipping EML part with an unreadable Content-Disposition: %s", exc)
                continue

            if disposition in ("attachment", "inline"):
                # Enforce the per-email cap BEFORE decoding the payload so a
                # malicious message with thousands of attachments can't make us
                # decode them all just to discard the overflow.
                if attachment_candidates >= _MAX_EML_ATTACHMENTS:
                    continue
                attachment_candidates += 1
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
                    body_content_type = content_type

        return body.strip(), body_content_type, attachments

    async def _render_body(
        self,
        document: Document,
        body: str,
        content_type: str,
    ) -> tuple[str, list[ImageBlock]]:
        if not body or content_type != "text/html":
            return body, []

        processed = await self._body_html_parser.parse(
            Document(
                id=document.id,
                filename=f"{document.filename or document.id}.html",
                content_type=DocumentType.HTML,
                text=body,
                metadata=dict(document.metadata),
            )
        )
        rendered = "\n\n".join(block.text for block in processed.text_blocks if block.text)
        return rendered, list(processed.images)

    async def _render_attachments(self, attachments: list[dict]) -> tuple[str, list[ImageBlock]]:
        """Render the attachment-section text and collect any ImageBlocks.

        Returns ``("", [])`` when there are no attachments.
        """
        if not attachments:
            return "", []

        rendered: list[str] = ["\n\n--- ATTACHMENTS ---\n"]
        images: list[ImageBlock] = []
        # One budget for the whole email, not one per attachment: 50 large
        # spreadsheets must not add up to 50 times the limit in one block.
        text_budget = _AttachmentTextBudget(_MAX_EML_ATTACHMENT_TEXT_BYTES)
        for att in attachments:
            ext = self._extension(att["filename"])
            header = (
                f"\nAttachment: {att['filename']}\nContent-Type: {att['content_type']}\nSize: {att['size']} bytes\n"
            )
            content, att_images = await self._render_one(att, ext, text_budget)
            rendered.append(header + content + "---\n")
            images.extend(att_images)
        return "".join(rendered), images

    async def _render_one(
        self, attachment: dict, ext: str, text_budget: _AttachmentTextBudget | None = None
    ) -> tuple[str, list[ImageBlock]]:
        """Dispatch one attachment. Returns ``(text_to_inline, image_blocks)``."""
        # An attachment's name decides how it is handled — which parser it
        # reaches, or whether it is emitted as an image for captioning — but
        # these bytes never crossed the upload check. Validate before that
        # decision, not inside one branch of it: an attachment with no
        # registered parser still falls through to the image path below.
        # A mismatch skips this attachment; the rest of the message parses.
        try:
            validate_content_matches_extension(ext, attachment["raw"][:CONTENT_SNIFF_BYTES])
            await asyncio.to_thread(validate_ooxml_package, ext, io.BytesIO(attachment["raw"]))
        except Exception as exc:
            logger.warning("Skipping attachment %s: %s", attachment["filename"], exc)
            return "", []

        parser = self._attachment_parsers.get(ext)
        if parser is not None:
            try:
                doc = Document(
                    filename=attachment["filename"],
                    raw_bytes=attachment["raw"],
                    content_type=Document.detect_content_type(attachment["filename"]),
                    metadata={"source": f"attachment:{attachment['filename']}"},
                )
                if hasattr(parser, "iter_batches"):
                    content = await asyncio.to_thread(self._render_tabular_attachment, parser, doc, text_budget)
                    images = []
                else:
                    processed = await parser.parse(doc)
                    content = "\n\n".join(b.text for b in processed.text_blocks if b.text)
                    images = list(processed.images)
                inline = f"Content:\n{content}\n" if content else ""
                return inline, images
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
    def _render_tabular_attachment(parser, document: Document, budget: _AttachmentTextBudget | None = None) -> str:
        """Render a tabular attachment incrementally within the email's text budget.

        The returned text, truncation marker included, never exceeds what the
        budget had left; the budget is then reduced by what was emitted.
        """
        budget = budget if budget is not None else _AttachmentTextBudget(_MAX_EML_ATTACHMENT_TEXT_BYTES)
        limit = budget.remaining
        rendered: list[str] = []
        used = 0
        truncated = False
        with closing(parser.iter_batches(document)) as batches:
            for batch in batches:
                if not batch.text:
                    continue
                encoded = batch.text.encode("utf-8")
                separator_bytes = len(b"\n\n") if rendered else 0
                available = limit - used - separator_bytes
                if len(encoded) > available:
                    if available > 0:
                        rendered.append(encoded[:available].decode("utf-8", errors="ignore"))
                    truncated = True
                    break
                rendered.append(batch.text)
                used += separator_bytes + len(encoded)
        text = "\n\n".join(rendered)
        if truncated:
            marker_bytes = len(_ATTACHMENT_TRUNCATED_MARKER.encode("utf-8"))
            # Make room for the marker (and its separator) inside the budget.
            room = limit - marker_bytes - len(b"\n\n")
            kept = text.encode("utf-8")[: max(room, 0)].decode("utf-8", errors="ignore").rstrip()
            if kept:
                text = f"{kept}\n\n{_ATTACHMENT_TRUNCATED_MARKER}"
            else:
                text = _ATTACHMENT_TRUNCATED_MARKER if marker_bytes <= limit else ""
        budget.remaining = max(limit - len(text.encode("utf-8")), 0)
        return text

    @staticmethod
    def _extension(filename: str) -> str:
        return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
