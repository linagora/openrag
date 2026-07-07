import asyncio
from pathlib import Path

import ray
import torch
from core.config import load_config
from core.indexing.parsers.document_parser import BasePooledParser
from core.models.document import (
    Document,
    DocumentType,
    ProcessedDocument,
    TextBlock,
)
from core.utils.logging import get_logger
from faster_whisper import WhisperModel

from ..ray_utils import call_ray_actor_with_timeout, retry_with_backoff

logger = get_logger()


def _whisper_num_gpus(config) -> float:
    """Return Whisper's Ray GPU reservation, falling back to CUDA detection.

    Mirrors Marker's GPU selection (see ``_marker_num_gpus``): the
    ``WhisperPool`` actor runs with ``num_gpus=0``, so Ray hides CUDA in its
    process and ``torch.cuda.is_available()`` returns False even on a GPU
    node — which would silently pin the ``WhisperActor``s to CPU. Ask whether
    the Ray cluster has GPU capacity instead, keeping the CUDA check only as a
    fallback for when Ray cannot report cluster resources yet.
    """
    requested_gpus = config.loader.local_whisper.whisper_num_gpus
    if requested_gpus <= 0:
        return 0
    try:
        return requested_gpus if ray.cluster_resources().get("GPU", 0) > 0 else 0
    except Exception as exc:
        logger.warning("Failed to query Ray cluster GPU resources; falling back to CUDA check", error=str(exc))
        return requested_gpus if torch.cuda.is_available() else 0


def whisper_actor_options(config) -> dict[str, float | int]:
    return {
        "num_gpus": _whisper_num_gpus(config),
        "max_restarts": 5,
        "max_concurrency": config.loader.local_whisper.whisper_concurrency_per_worker,
    }


# Duration of the audio sample used for language detection
LANG_DETECT_SAMPLE_MS = 30_000  # 30 s


@ray.remote
class WhisperActor:
    def __init__(self):
        import torch
        from core.config import load_config
        from core.utils.logging import get_logger

        self.logger = get_logger()
        self.config = load_config()

        device = "cuda" if torch.cuda.is_available() else "cpu"
        compute_type = "float16" if device == "cuda" else "int8"
        model_name = self.config.loader.local_whisper.model

        self.logger.info("Loading Whisper model", model_name=model_name, device=device, compute_type=compute_type)
        self.model = WhisperModel(model_name, device=device, compute_type=compute_type)
        self.logger.info("Whisper model loaded successfully", model_name=model_name, device=device)

    async def transcribe(self, wav_path: str | Path) -> str:
        self.logger.info("Transcribing audio file", file_path=Path(wav_path).name)

        def _transcribe_sync() -> str:
            segments, _ = self.model.transcribe(str(wav_path))
            return "".join(segment.text for segment in segments)

        return await asyncio.to_thread(_transcribe_sync)

    async def detect_language(
        self,
        file_path: str | Path,
        fallback_language: str = "en",
        sample_ms: int = LANG_DETECT_SAMPLE_MS,
    ) -> str:
        import tempfile

        from pydub import AudioSegment

        file_path = Path(file_path)
        self.logger.info("Detecting language for audio file", file_path=file_path.name)
        fd, tmp_path = tempfile.mkstemp(prefix=f"{file_path.stem}_langdetect_", suffix=".wav")
        try:

            def _prepare_and_detect() -> str:
                import os

                os.close(fd)
                sound = AudioSegment.from_file(file_path)
                sample = sound[:sample_ms]
                sample.export(tmp_path, format="wav")
                _, info = self.model.transcribe(tmp_path, beam_size=1, max_new_tokens=1)
                return info.language

            return await asyncio.to_thread(_prepare_and_detect)
        except Exception:
            self.logger.exception("Error detecting language", file_path=file_path.name)
            return fallback_language
        finally:
            Path(tmp_path).unlink(missing_ok=True)


@ray.remote
class WhisperPool:
    """Ray-actor pool of ``WhisperActor``s. Internal — the public
    ``BasePooledParser`` face is ``LocalWhisperLoader``.
    """

    def __init__(self):
        from core.config import load_config
        from core.utils.logging import get_logger

        self.logger = get_logger()
        self.config = load_config()

        n_workers = self.config.loader.local_whisper.whisper_n_workers
        self.logger.info(f"Starting WhisperPool with {n_workers} workers")
        self.workers = [WhisperActor.options(**whisper_actor_options(self.config)).remote() for _ in range(n_workers)]
        self._pending = [0] * n_workers

    async def _transcribe_chunk(self, idx: int, path):
        return await call_ray_actor_with_timeout(
            self.workers[idx].transcribe.remote(path),
            timeout=self.config.loader.local_whisper.whisper_timeout,
            task_description=f"WhisperPool transcribe ({path})",
        )

    async def transcribe(self, path):
        async def attempt(_i: int):
            idx = min(range(len(self._pending)), key=lambda j: self._pending[j])
            self._pending[idx] += 1
            try:
                return await self._transcribe_chunk(idx, path)
            finally:
                self._pending[idx] -= 1

        return await retry_with_backoff(
            attempt,
            max_retries=self.config.loader.local_whisper.whisper_max_task_retry,
            base_delay=self.config.loader.local_whisper.whisper_retry_base_delay,
            task_description=f"WhisperPool transcribe ({path})",
        )


async def detect_language_via_actor(
    file_path: str | Path,
    *,
    fallback_language: str = "en",
    timeout: float | None = None,
) -> str | None:
    """Detect the spoken language of ``file_path`` via the singleton WhisperActor.

    Wraps the ``.remote()`` call so non-worker code (the OpenAI audio
    loader's optional language detector) can stay Ray-free. Returns
    ``None`` on failure so callers can fall back to a default behaviour.
    """
    config = load_config()
    try:
        actor = WhisperActor.options(
            name="WhisperActor",
            namespace="openrag",
            get_if_exists=True,
            **whisper_actor_options(config),
        ).remote()
    except Exception:
        logger.exception("Error getting WhisperActor")
        return None

    effective_timeout = timeout if timeout is not None else config.loader.local_whisper.whisper_timeout
    try:
        return await call_ray_actor_with_timeout(
            actor.detect_language.remote(file_path, fallback_language),
            timeout=effective_timeout,
            task_description=f"WhisperActor detect_language ({Path(file_path).name})",
        )
    except Exception:
        logger.exception("Language detection failed", file_path=str(file_path))
        return None


class LocalWhisperLoader(BasePooledParser):
    """Public ``BasePooledParser`` facade for the local-Whisper Ray pool.

    Holds a handle to the named ``WhisperPool`` Ray actor and dispatches
    each ``parse()`` call to it. Whisper requires a file path on disk,
    so ``Document.raw_bytes`` is written to a NamedTemporaryFile before
    handoff.
    """

    def __init__(self):
        self.config = load_config()
        # Lazily create the pool if bootstrap didn't — mirrors MarkerLoader so
        # audio ingestion self-provisions WhisperPool instead of relying on the
        # eager init_audio_actor() pre-warm at startup.
        from services.workers.bootstrap import get_or_create_actor

        self.worker: WhisperPool = get_or_create_actor("WhisperPool", WhisperPool, lifetime="detached")

    def supported_types(self) -> list[str]:
        return [DocumentType.AUDIO.value, DocumentType.VIDEO.value]

    async def parse(self, document: Document) -> ProcessedDocument:
        if not document.raw_bytes:
            return ProcessedDocument(
                document_id=document.id,
                metadata=dict(document.metadata),
            )

        async with document.as_temporary_file() as path:
            try:
                text = await self.worker.transcribe.remote(str(path))
            except Exception as e:
                logger.error("Error transcribing audio", error=str(e))
                raise

        text_blocks = [TextBlock(text=text, page_number=1)] if text else []
        return ProcessedDocument(
            document_id=document.id,
            text_blocks=text_blocks,
            metadata=dict(document.metadata),
            page_count=1 if text else 0,
        )
