"""Tests for EmlParser — focused on the parser-bomb fan-out cap."""

from __future__ import annotations

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
