from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any

from core.models.document import ImageBlock, ProcessedDocument, TextBlock
from core.prompts.vlm_prompt_builder import wrap_caption
from core.vlm.vlm import VLM
from services.workers.stages._common import run_with_optional_timeout, scrub_credentials, stage_timeout


async def caption_stage(
    row: MutableMapping[str, Any],
    vlm: VLM,
    *,
    timeout: float | None = None,
    per_image_timeout: float = 0.0,
) -> MutableMapping[str, Any]:
    """Caption images in ``row["processed_document"]`` and mutate the row."""

    try:
        processed_document = row.get("processed_document")
        if not isinstance(processed_document, ProcessedDocument):
            raise ValueError("caption_stage row must contain a ProcessedDocument under 'processed_document'")

        prompt = row.get("caption_prompt")
        if prompt is not None:
            prompt = str(prompt)

        effective_timeout = stage_timeout(timeout, len(processed_document.images), per_item_timeout=per_image_timeout)
        row["processed_document"] = await run_with_optional_timeout(
            lambda: _caption_document(processed_document, vlm, prompt),
            effective_timeout,
        )
        row["stage"] = "captioned"
        row.pop("error", None)
        return row
    except Exception as exc:
        row["stage"] = "caption_failed"
        row["error"] = str(exc)
        raise
    finally:
        scrub_credentials(row)


async def _caption_document(
    processed_document: ProcessedDocument,
    vlm: VLM,
    prompt: str | None,
) -> ProcessedDocument:
    """Return a copy of ``processed_document`` with captions materialized."""
    text_blocks = list(processed_document.text_blocks)
    captioned_images: list[ImageBlock] = []

    for image in processed_document.images:
        caption = await vlm.caption_image(image.image_bytes, prompt=prompt)
        wrapped = wrap_caption(caption)
        captioned_image = image.model_copy(update={"caption": caption})
        captioned_images.append(captioned_image)
        if not _replace_markdown_ref(text_blocks, image, wrapped):
            text_blocks.append(TextBlock(text=wrapped, page_number=image.page_number))

    return processed_document.model_copy(
        update={
            "text_blocks": text_blocks,
            "images": captioned_images,
        }
    )


def _replace_markdown_ref(text_blocks: list[TextBlock], image: ImageBlock, wrapped_caption: str) -> bool:
    """Replace an image markdown placeholder in text blocks when one exists."""
    markdown_ref = image.metadata.get("markdown_ref")
    if not markdown_ref:
        return False

    replaced = False
    for index, block in enumerate(text_blocks):
        if markdown_ref not in block.text:
            continue
        text_blocks[index] = block.model_copy(update={"text": block.text.replace(markdown_ref, wrapped_caption)})
        replaced = True
    return replaced
