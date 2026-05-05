"""Tests for the VLM (image-captioning) prompt builder."""

from __future__ import annotations

from openrag.core.prompts.vlm_prompt_builder import (
    IMAGE_DESCRIPTION_CLOSE,
    IMAGE_DESCRIPTION_OPEN,
    build_caption_messages,
    wrap_caption,
)


def test_build_caption_messages_shapes_for_openai_multimodal():
    msgs = build_caption_messages(template="Describe this image.", image_url="https://example.com/x.png")
    assert len(msgs) == 1
    msg = msgs[0]
    assert msg["role"] == "user"
    parts = msg["content"]
    assert {p["type"] for p in parts} == {"image_url", "text"}
    image_part = next(p for p in parts if p["type"] == "image_url")
    text_part = next(p for p in parts if p["type"] == "text")
    assert image_part["image_url"] == {"url": "https://example.com/x.png"}
    assert text_part["text"] == "Describe this image."


def test_build_caption_messages_supports_data_uri():
    msgs = build_caption_messages(template="caption", image_url="data:image/png;base64,abc")
    image_part = next(p for p in msgs[0]["content"] if p["type"] == "image_url")
    assert image_part["image_url"]["url"].startswith("data:image/png;base64,")


def test_wrap_caption_uses_image_description_markers():
    out = wrap_caption("a sunset over the sea")
    assert out.startswith(IMAGE_DESCRIPTION_OPEN)
    assert out.endswith(IMAGE_DESCRIPTION_CLOSE)
    assert "a sunset over the sea" in out


def test_wrap_caption_format_is_load_bearing_for_chunker():
    """The chunker's image-element regex matches `<image_description>...</image_description>`,
    so this exact pairing is part of the contract."""
    out = wrap_caption("x")
    assert "<image_description>" in out
    assert "</image_description>" in out
