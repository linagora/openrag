import asyncio
from pathlib import Path

import ray
from services.utils import get_audio_semaphore
from langchain_core.documents.base import Document
from openai import AsyncOpenAI
from pydub import AudioSegment
from utils.logger import get_logger

from ..base import BaseLoader
from .local_whisper import WhisperActor

logger = get_logger()

# Duration of the audio sample used for language detection
LANG_DETECT_SAMPLE_MS = 30_000  # 30 s


class AudioTranscriber:
    """Transcribes audio in a single request (no chunking).

    Language detection is handled locally by WhisperActor (faster-whisper).
    vLLM's native language detection fix is not yet merged (PR #34342) missed the v0.16.0 branch
    cut (Feb 8) — it was merged Feb 21 and will ship in v0.17.0.
    """

    def __init__(self, config):
        self.client = AsyncOpenAI(
            base_url=config.loader.transcriber.base_url,
            api_key=config.loader.transcriber.api_key,
            timeout=config.loader.transcriber.timeout,
        )
        self.model_name = config.loader.transcriber.model_name
        self.use_whisper_lang_detector = config.loader.transcriber.get("use_whisper_lang_detector", True)

    async def transcribe(self, file_path: Path) -> str:
        # vLLM uses soundfile/libsndfile internally, which only supports WAV/FLAC/OGG.
        # mp3, mp4, m4a, etc. raise "Format not recognised", so convert to WAV first.

        try:
            logger.bind(file=file_path.name)
            if file_path.suffix.lower() == ".wav":
                wav_path = file_path
                tmp_wav = None
                sound = await asyncio.to_thread(AudioSegment.from_file, wav_path)
            else:
                sound = await asyncio.to_thread(AudioSegment.from_file, file_path)
                logger.info("Converting audio to WAV", duration_s=f"{len(sound) / 1000:.1f}")
                tmp_wav = file_path.with_suffix(".wav")
                await asyncio.to_thread(sound.export, tmp_wav, format="wav")
                wav_path = tmp_wav

            language = await self._detect_language(sound, wav_path) if self.use_whisper_lang_detector else None
            logger.info("Transcribing audio as a single request", language=language)

            async with get_audio_semaphore():
                return await self._transcribe_file(wav_path, language)
        except Exception as e:
            logger.exception("Error in transcribe", error=str(e))
            raise e
        finally:
            if tmp_wav:
                await asyncio.to_thread(tmp_wav.unlink, True)

    async def _detect_language(self, sound: AudioSegment, wav_path: Path, fallback: str = "en") -> str:
        """Detect language via local WhisperActor from a short audio sample."""
        sample = sound[:LANG_DETECT_SAMPLE_MS]
        tmp_path = wav_path.parent / f"{wav_path.stem}_langdetect.wav"
        await asyncio.to_thread(sample.export, tmp_path, format="wav")
        try:
            whisper_actor = self._get_whisper_actor()
            return await whisper_actor.detect_language.remote(tmp_path, fallback)
        except Exception as e:
            logger.exception("Language detection failed", error=str(e))
            return fallback
        finally:
            await asyncio.to_thread(tmp_path.unlink, True)

    def _get_whisper_actor(self):
        actor_name = "WhisperActor"
        try:
            return ray.get_actor(actor_name, namespace="openrag")
        except ValueError:
            return WhisperActor.options(name=actor_name, namespace="openrag").remote()
        except Exception as e:
            logger.error("Error getting WhisperActor", error=str(e))
            raise

    async def _transcribe_file(self, wav_path: Path, language: str = None) -> str:
        """Send a single file to the transcription endpoint."""
        try:
            kwargs = {"model": self.model_name, "file": wav_path}
            if language:
                kwargs["language"] = language
            result = await self.client.audio.transcriptions.create(**kwargs)
            return result.text
        except Exception as e:
            logger.exception("Error transcribing file", file=wav_path.name, error=str(e))
            raise e


class OpenAIAudioLoader(BaseLoader):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.transcriber = AudioTranscriber(config=self.config)

    async def aload_document(self, file_path, metadata: dict = None, save_markdown=False):
        try:
            content = await self.transcriber.transcribe(Path(file_path))
            doc = Document(page_content=content, metadata=metadata)
            if save_markdown:
                self.save_content(content, str(file_path))
            return doc
        except Exception as e:
            logger.exception("Error in OpenAIAudioLoader", path=file_path, error=str(e))
            raise e
