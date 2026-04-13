import asyncio
from pathlib import Path

import ray
import torch
from config import load_config
from faster_whisper import WhisperModel
from langchain_core.documents.base import Document
from utils.logger import get_logger

from ..base import BaseLoader

logger = get_logger()
config = load_config()


if torch.cuda.is_available():
    WHISPER_NUM_GPUS = config.loader.local_whisper.whisper_num_gpus
else:  # On CPU
    WHISPER_NUM_GPUS = 0

WHISPER_CONCURRENCY_PER_WORKER = config.loader.local_whisper.whisper_concurrency_per_worker


@ray.remote(
    num_gpus=WHISPER_NUM_GPUS, max_restarts=5, max_concurrency=WHISPER_CONCURRENCY_PER_WORKER
)  # Ensure each worker processes one file at a time
class WhisperActor:
    def __init__(self):
        import torch
        from config import load_config
        from utils.logger import get_logger

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

    async def detect_language(self, wav_path: str | Path, fallback_language="en") -> str:
        try:
            self.logger.info("Detecting language for audio file", file_path=Path(wav_path).name)

            def _detect_language_sync() -> str:
                # beam_size=1 + max_new_tokens=1 runs only language detection, no full transcription
                _, info = self.model.transcribe(str(wav_path), beam_size=1, max_new_tokens=1)
                return info.language

            return await asyncio.to_thread(_detect_language_sync)

        except Exception as e:
            self.logger.error("Error detecting language", error=str(e))
            return fallback_language


@ray.remote
class WhisperPool:
    def __init__(self):
        from utils.logger import get_logger

        self.logger = get_logger()

        n_workers = config.loader.local_whisper.whisper_n_workers
        self.logger.info(f"Starting WhisperPool with {n_workers} workers")
        self.workers = [WhisperActor.remote() for _ in range(n_workers)]
        self._pending = [0] * n_workers

    async def transcribe(self, path):
        idx = min(range(len(self._pending)), key=lambda i: self._pending[i])
        self._pending[idx] += 1
        try:
            return await self.workers[idx].transcribe.remote(path)
        finally:
            self._pending[idx] -= 1


class LocalWhisperLoader(BaseLoader):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.whisper_actor: WhisperPool = ray.get_actor("WhisperPool", namespace="openrag")

    async def aload_document(self, file_path, metadata: dict = None, save_markdown=False):
        try:
            content = await self.whisper_actor.transcribe.remote(file_path)
            doc = Document(page_content=content, metadata=metadata)
            if save_markdown:
                self.save_content(content, str(file_path))
            return doc
        except Exception as e:
            self.logger.error("Error loading document", error=str(e))
            raise e
