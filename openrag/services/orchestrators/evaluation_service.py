"""EvaluationService — datasets on disk, runs dispatched to the worker layer.

This slice covers dataset storage: an admin uploads a corpus plus a test set,
both land under ``<data_dir>/eval/<dataset_id>/``, and a row records what is
there. Run dispatch follows.
"""

from __future__ import annotations

import asyncio
import shutil
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from core.evaluation import parse_testset
from core.models.evaluation import EvalDataset
from core.utils.exceptions import ConflictError, NotFoundError, ValidationError

if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import IO

    from core.config.root import Settings
    from core.ports.evaluation_repo import EvaluationRepository

TESTSET_FILENAME = "testset.csv"
CORPUS_DIRNAME = "corpus"

#: Block size for streaming an upload to disk.
_COPY_CHUNK_BYTES = 1024 * 1024


class EvaluationService:
    """Dataset storage plus run dispatch for the admin evaluation page."""

    def __init__(
        self,
        *,
        repo: EvaluationRepository,
        config: Settings,
    ) -> None:
        self._repo = repo
        self._config = config
        self._settings = config.evaluation
        self._root = Path(config.paths.data_dir) / "eval"

    # ── datasets ─────────────────────────────────────────────────────

    def _dataset_dir(self, dataset_id: str) -> Path:
        return self._root / dataset_id

    async def create_dataset(
        self,
        *,
        name: str,
        corpus: Sequence[tuple[str, IO[bytes]]],
        testset: IO[bytes],
        user_id: int | None,
    ) -> EvalDataset:
        """Validate and store a corpus + test set.

        The CSV is parsed here so a malformed test set is rejected at upload
        rather than after a run has already spent minutes indexing.

        Uploads arrive as open binary streams rather than ``bytes``: each is
        read under a size cap and copied to disk in fixed-size blocks, so a
        large corpus is never held in memory. The blocking file I/O runs on a
        worker thread so it cannot stall the event loop.
        """
        if not name.strip():
            raise ValidationError("Dataset name is required.", status_code=400)
        if not corpus:
            raise ValidationError("At least one corpus file is required.", status_code=400)

        testset_csv = await asyncio.to_thread(
            self._read_capped,
            testset,
            self._settings.max_testset_bytes,
            f"Test set exceeds the {self._settings.max_testset_mb} MB limit.",
        )
        cases = parse_testset(testset_csv, max_rows=self._settings.max_testset_rows)

        dataset_id = uuid.uuid4().hex
        directory = self._dataset_dir(dataset_id)
        try:
            written = await asyncio.to_thread(self._store_upload, directory, corpus, testset_csv)
            return await self._repo.create_dataset(
                EvalDataset(
                    id=dataset_id,
                    name=name.strip(),
                    corpus_file_count=written,
                    testset_row_count=len(cases),
                    created_by=user_id,
                )
            )
        except Exception:
            await asyncio.to_thread(shutil.rmtree, directory, True)
            raise

    @staticmethod
    def _read_capped(stream: IO[bytes], limit: int, message: str) -> bytes:
        """Read a stream, refusing anything past ``limit``.

        Reads one byte beyond the cap rather than trusting a client-supplied
        length, so an inflated ``Content-Length`` cannot get past it.
        """
        stream.seek(0)
        payload = stream.read(limit + 1)
        if len(payload) > limit:
            raise ValidationError(message, status_code=413)
        return payload

    def _store_upload(
        self,
        directory: Path,
        corpus: Sequence[tuple[str, IO[bytes]]],
        testset_csv: bytes,
    ) -> int:
        """Write the corpus and test set to disk. Blocking; call in a thread."""
        corpus_dir = directory / CORPUS_DIRNAME
        corpus_dir.mkdir(parents=True, exist_ok=True)

        written = 0
        budget = self._settings.max_corpus_bytes
        for filename, stream in corpus:
            # Flatten any path components a browser may have sent.
            target = corpus_dir / Path(filename).name
            if target.exists():
                raise ValidationError(
                    f"Corpus contains more than one file named '{target.name}'.",
                    status_code=400,
                )
            budget -= self._copy_within_budget(stream, target, budget)
            written += 1

        (directory / TESTSET_FILENAME).write_bytes(testset_csv)
        return written

    def _copy_within_budget(self, stream: IO[bytes], target: Path, budget: int) -> int:
        """Copy ``stream`` into ``target``, refusing to exceed ``budget``."""
        stream.seek(0)
        written = 0
        with target.open("wb") as handle:
            while chunk := stream.read(_COPY_CHUNK_BYTES):
                written += len(chunk)
                if written > budget:
                    raise ValidationError(
                        f"Corpus exceeds the {self._settings.max_corpus_mb} MB limit.",
                        status_code=413,
                    )
                handle.write(chunk)
        return written

    async def list_datasets(self) -> list[EvalDataset]:
        return await self._repo.list_datasets()

    async def delete_dataset(self, dataset_id: str) -> None:
        """Delete a dataset and its stored files.

        Refused while a run is using it: the runner reads the corpus from disk
        for the whole indexing phase, so removing it mid-run would surface as a
        confusing FileNotFoundError instead of a clear conflict.
        """
        active = await self._repo.active_run()
        if active is not None and active.dataset_id == dataset_id:
            raise ConflictError(f"Evaluation run '{active.id}' is still using this dataset.")

        if not await self._repo.delete_dataset(dataset_id):
            raise NotFoundError(f"Evaluation dataset '{dataset_id}' not found")
        await asyncio.to_thread(shutil.rmtree, self._dataset_dir(dataset_id), True)


__all__ = ["EvaluationService"]
