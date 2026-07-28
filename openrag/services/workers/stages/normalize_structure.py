"""Fail-open structural normalization stage for PDF indexing."""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any

from core.config.table_reconstruction import TableReconstructionConfig
from core.indexing.structure_normalizer import DocumentStructureNormalizer
from core.models.document import Document, NormalizationReport, ProcessedDocument
from core.utils.logging import get_logger
from services.workers.stages._common import run_with_optional_timeout, scrub_credentials

logger = get_logger()


async def normalize_structure_stage(
    row: MutableMapping[str, Any],
    normalizer: DocumentStructureNormalizer,
    config: TableReconstructionConfig,
    *,
    timeout: float | None = None,
) -> MutableMapping[str, Any]:
    """Normalize document structure while preserving usable output on failure."""
    try:
        document = row.get("document")
        processed_document = row.get("processed_document")
        if not isinstance(document, Document):
            raise ValueError("normalize_structure_stage row must contain a Document under 'document'")
        if not isinstance(processed_document, ProcessedDocument):
            raise ValueError(
                "normalize_structure_stage row must contain a ProcessedDocument under 'processed_document'"
            )

        async def run() -> ProcessedDocument:
            return await normalizer.normalize(document, processed_document, config)

        row["processed_document"] = await run_with_optional_timeout(run, timeout)
        row["stage"] = "structure_normalized"
        row.pop("error", None)
        return row
    except Exception as exc:  # noqa: BLE001 - automatic mode is explicitly fail-open
        processed_document = row.get("processed_document")
        if isinstance(processed_document, ProcessedDocument):
            row["processed_document"] = processed_document.model_copy(
                update={
                    "normalized_text_blocks": None,
                    "normalization_report": NormalizationReport(
                        algorithm_version=config.algorithm_version,
                        status="partial_fallback",
                        fallback_reasons=[f"{type(exc).__name__}: {exc}"],
                    ),
                }
            )
        logger.bind(
            task_id=row.get("task_id"),
            filename=row.get("filename", ""),
            error_type=type(exc).__name__,
        ).warning("Structural normalization failed open; preserving parser output")
        row["stage"] = "structure_normalization_fallback"
        row.pop("error", None)
        return row
    finally:
        scrub_credentials(row)


__all__ = ["normalize_structure_stage"]
