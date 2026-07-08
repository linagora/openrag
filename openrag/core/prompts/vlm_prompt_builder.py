"""VLM (vision-language model) prompt builder.

Pure helpers: given a captioning template and an image reference, produce a
multimodal chat-message payload (OpenAI-style) and wrap captions in the
``<image_description>`` markers downstream pipelines expect.
"""

from __future__ import annotations

from typing import Any

from core.utils.conts import IMG_WRAPPER_CLOSE, IMG_WRAPPER_OPEN


def build_caption_messages(template: str, image_url: str) -> list[dict[str, Any]]:
    """Build a multimodal chat-message list for image captioning.

    Args:
        template: Image-captioning prompt text (no substitution required).
        image_url: ``https://...`` URL or ``data:image/...;base64,...`` data URI.

    Returns:
        A single-message list shaped for OpenAI / vLLM chat completions:
        ``[{"role": "user", "content": [{"type": "image_url", ...}, {"type": "text", ...}]}]``
    """
    return [
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": image_url}},
                {"type": "text", "text": template},
            ],
        }
    ]


def wrap_caption(caption: str) -> str:
    """Wrap a raw caption in ``<image_description>`` markers.

    Pipelines downstream (markdown image replacement, chunk parsing) look for
    this exact marker, so the wrapping format is part of the contract.
    """
    return f"{IMG_WRAPPER_OPEN}\n\n{caption}\n\n{IMG_WRAPPER_CLOSE}"
