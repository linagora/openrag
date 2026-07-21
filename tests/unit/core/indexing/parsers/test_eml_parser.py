"""Tests for EmlParser — focused on the parser-bomb fan-out cap."""

from __future__ import annotations

import base64
import email.message

import pytest
from core.indexing.parsers.eml_parser import _MAX_EML_ATTACHMENTS, EmlParser
from core.models.document import Document, DocumentType


def _eml_with_attachments(n: int) -> bytes:
    msg = email.message.EmailMessage()
    msg["Subject"] = "bomb"
    msg["From"] = "a@b.com"
    msg["To"] = "c@d.com"
    msg.set_content("body text")
    for i in range(n):
        msg.add_attachment(
            b"x" * 8,
            maintype="application",
            subtype="octet-stream",
            filename=f"file{i}.bin",
        )
    return msg.as_bytes()


def _eml_with_unnamed_attachments(n: int) -> email.message.EmailMessage:
    msg = email.message.EmailMessage()
    msg["Subject"] = "malformed bomb"
    msg["From"] = "a@b.com"
    msg["To"] = "c@d.com"
    msg.set_content("body text")
    msg.make_mixed()
    for _ in range(n):
        part = email.message.EmailMessage()
        part.set_content("payload")
        part.replace_header("Content-Type", "application/octet-stream")
        part["Content-Disposition"] = "attachment"
        msg.attach(part)
    return msg


@pytest.mark.asyncio
async def test_eml_caps_attachment_fanout():
    # A malicious email with far more than the cap must process at most the cap.
    raw = _eml_with_attachments(_MAX_EML_ATTACHMENTS + 25)
    doc = Document(filename="bomb.eml", content_type=DocumentType.EML, raw_bytes=raw)
    processed = await EmlParser().parse(doc)
    assert processed.metadata["email_attachment_count"] == _MAX_EML_ATTACHMENTS


@pytest.mark.asyncio
async def test_eml_under_cap_processes_all():
    raw = _eml_with_attachments(3)
    doc = Document(filename="ok.eml", content_type=DocumentType.EML, raw_bytes=raw)
    processed = await EmlParser().parse(doc)
    assert processed.metadata["email_attachment_count"] == 3


@pytest.mark.asyncio
async def test_html_only_body_extracts_images_without_leaking_base64():
    encoded = base64.b64encode(b"email-image").decode()
    msg = email.message.EmailMessage()
    msg["Subject"] = "HTML report"
    msg.set_content(
        f'<p>Report</p><img alt="chart" src="data:image/png;base64,{encoded}">',
        subtype="html",
    )

    processed = await EmlParser().parse(
        Document(filename="report.eml", content_type=DocumentType.EML, raw_bytes=msg.as_bytes())
    )

    assert len(processed.images) == 1
    assert processed.images[0].image_bytes == b"email-image"
    assert "data:image" not in processed.text_blocks[0].text


@pytest.mark.asyncio
async def test_plain_text_alternative_remains_preferred_over_html_body():
    encoded = base64.b64encode(b"unused-image").decode()
    msg = email.message.EmailMessage()
    msg["Subject"] = "Alternative report"
    msg.set_content("Plain report")
    msg.add_alternative(
        f'<p>HTML report</p><img src="data:image/png;base64,{encoded}">',
        subtype="html",
    )

    processed = await EmlParser().parse(
        Document(filename="report.eml", content_type=DocumentType.EML, raw_bytes=msg.as_bytes())
    )

    assert processed.images == []
    assert processed.text_blocks[0].text == "Plain report"


def test_eml_caps_attachment_candidates_before_decoding(monkeypatch):
    msg = _eml_with_unnamed_attachments(_MAX_EML_ATTACHMENTS + 25)
    original_get_payload = email.message.Message.get_payload
    attachment_decodes = 0

    def counting_get_payload(self, *args, **kwargs):
        nonlocal attachment_decodes
        if kwargs.get("decode") and self.get_content_disposition() in ("attachment", "inline"):
            attachment_decodes += 1
        return original_get_payload(self, *args, **kwargs)

    monkeypatch.setattr(email.message.Message, "get_payload", counting_get_payload)

    _, attachments = EmlParser._walk_parts(msg)

    assert attachments == []
    assert attachment_decodes == _MAX_EML_ATTACHMENTS
