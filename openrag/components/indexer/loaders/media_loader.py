import asyncio
import os
from pathlib import Path

import langdetect
import librosa
import numpy as np
from components.utils import get_audio_semaphore
from langchain_core.documents.base import Document
from openai import AsyncOpenAI, APIError
from pydub import AudioSegment, silence
from tqdm.asyncio import tqdm
from utils.logger import get_logger

from .base import BaseLoader

logger = get_logger()

MEDIA_FORMATS = [".wav", ".mp3", ".mp4", ".ogg", ".flv", ".wma", ".aac"]


class AudioTranscriber:
    def __init__(self, config):
        self.client = AsyncOpenAI(
            base_url=config.loader.transcriber.base_url,
            api_key=config.loader.transcriber.api_key,
            timeout=config.loader.transcriber.timeout,
        )
        self.model_name = config.loader.transcriber.model_name
        self.max_chunk_ms = config.loader.transcriber.max_chunk_ms
        self.silence_thresh_db = config.loader.transcriber.silence_thresh_db
        self.min_silence_len_ms = config.loader.transcriber.min_silence_len_ms

    async def transcribe(self, wav_path: Path) -> str:
        sound = await asyncio.to_thread(AudioSegment.from_wav, wav_path)
        total_ms = len(sound)

        logger.info(f"Analyzing audio length {total_ms / 1000:.1f}s")

        # Split into chunks
        chunks = await self._get_audio_chunks(sound)
        logger.info(f"Detected {len(chunks)} chunks")

        # Detect language
        language = await self._detect_language(sound[chunks[0][0] : chunks[0][1]], wav_path)
        logger.info(f"Detected language: {language}")

        # Create tasks for each chunk
        tasks = [self._process_chunk(i, sound[start:end], wav_path, language) for i, (start, end) in enumerate(chunks)]
        texts = await tqdm.gather(*tasks, desc="Transcribing audio chunks")
        return self._stitch_transcriptions(texts)

    async def _process_chunk(self, index: int, segment: AudioSegment, wav_path: Path, language: str = None) -> str:
        """Export a segment, transcribe it, and clean up."""
        async with get_audio_semaphore():
            tmp_path = wav_path.parent / f"{wav_path.stem}_chunk_{index:03d}.wav"
            await asyncio.to_thread(segment.export, tmp_path, format="wav")
            try:
                result = await self._transcribe_chunk(tmp_path, language)
                return result
            except APIError as e:
                # OpenAI API errors - gracefully degrade by returning empty transcript
                logger.warning("Audio transcription API error", chunk=tmp_path.name, error=str(e)[:200])
                return ""
            except Exception as e:
                # Unexpected errors - log and return empty transcript for graceful degradation
                logger.exception("Error transcribing chunk", chunk=tmp_path.name, error=str(e))
                return ""
            finally:
                tmp_path.unlink(missing_ok=True)

    async def _transcribe_chunk(self, wav_path: Path, language: str = None) -> str:
        """Transcribe a single WAV chunk."""
        try:
            logger.debug(f"Transcribing chunk: {wav_path.name}")

            kwargs = {
                "model": self.model_name,
                "file": wav_path,
            }
            if language:
                kwargs["language"] = language
            result = await self.client.audio.transcriptions.create(**kwargs)
            return result.text.strip()
        except APIError as e:
            # OpenAI API errors - gracefully degrade by returning empty transcript
            logger.warning("Audio transcription API error", chunk=wav_path.name, error=str(e)[:200])
            return ""
        except Exception as e:
            # Unexpected errors - log and return empty transcript for graceful degradation
            logger.exception("Error transcribing chunk", chunk=wav_path.name, error=str(e))
            return ""

    async def _get_audio_chunks(self, sound: AudioSegment) -> list[AudioSegment]:
        """Split audio into chunks based on silence detection."""
        total_ms = len(sound)
        if total_ms <= self.max_chunk_ms:
            return [(0, total_ms)]

        logger.debug("Detecting silences for chunking...")
        downsampled_sound = sound.set_channels(1).set_frame_rate(16000)

        # Reduce analysis resolution
        silences = await asyncio.to_thread(self._detect_silences_librosa, downsampled_sound)
        chunks = []
        start = 0
        while start < total_ms:
            target_end = start + self.max_chunk_ms
            if target_end >= total_ms:
                end = total_ms
            else:
                valid_silences = [s for s in silences if start < s[0] < target_end]
                if valid_silences:
                    end = valid_silences[-1][0]
                else:
                    end = target_end
            chunks.append((start, end))
            start = end

        return chunks

    def _stitch_transcriptions(self, texts: list[str]) -> str:
        """Concatenate with spacing."""
        return "\n".join(t.strip() for t in texts if t.strip())

    async def _detect_language(self, sound: AudioSegment, wav_path, fallback_language="en") -> str:
        """Detect the language of the audio segment."""
        tmp_path = wav_path.parent / f"{wav_path.stem}_langdetect.wav"
        await asyncio.to_thread(sound.export, tmp_path, format="wav")
        text = await self._transcribe_chunk(tmp_path)
        if not text:
            return fallback_language  # Fallback to English
        try:
            return langdetect.detect(text)
        except langdetect.LangDetectException as e:
            # Expected failure for non-textual content or too-short text
            logger.warning("Language detection failed", error=str(e))
            return fallback_language
        except Exception as e:
            # Unexpected language detection errors
            logger.exception("Language detection failed", error=str(e))
            return fallback_language
        finally:
            await asyncio.to_thread(os.remove, tmp_path)

    def _detect_silences_pydub(self, sound: AudioSegment) -> list[tuple[int, int]]:
        """Silence detection using pydub (slower)."""

        silences = silence.detect_silence(
            sound,
            min_silence_len=self.min_silence_len_ms,
            silence_thresh=self.silence_thresh_db,
        )
        silences = [(start, end) for start, end in silences]
        return silences

    def _detect_silences_librosa(self, sound: AudioSegment) -> list[tuple[int, int]]:
        """Faster silence detection using librosa."""
        # Convert to numpy array
        samples = np.array(sound.get_array_of_samples())
        # Preserve original dtype for normalization
        original_dtype = samples.dtype

        if sound.channels == 2:
            samples = samples.reshape((-1, 2))
            samples = samples.mean(axis=1)

        # Normalize
        samples = samples.astype(np.float32) / np.iinfo(original_dtype).max

        # Use librosa's onset detection (inverted for silence)
        frame_length = int(self.min_silence_len_ms * sound.frame_rate / 1000)
        hop_length = frame_length // 4

        # Calculate RMS energy
        rms = librosa.feature.rms(y=samples, frame_length=frame_length, hop_length=hop_length)[0]

        # Convert threshold from dB to amplitude
        threshold = 10 ** (self.silence_thresh_db / 20)

        # Find silent frames
        silent_frames = rms < threshold

        # Convert frames to time ranges
        silences = []
        in_silence = False
        silence_start = 0

        for i, is_silent in enumerate(silent_frames):
            time_ms = int(i * hop_length * 1000 / sound.frame_rate)
            if is_silent and not in_silence:
                silence_start = time_ms
                in_silence = True
            elif not is_silent and in_silence:
                silences.append((silence_start, time_ms))
                in_silence = False

        if in_silence:
            silences.append((silence_start, len(sound)))

        return silences


class VideoAudioLoader(BaseLoader):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.transcriber = AudioTranscriber(config=self.config)

    async def aload_document(self, file_path, metadata: dict = None, save_markdown=False):
        path = Path(file_path)
        if path.suffix not in MEDIA_FORMATS:
            logger.warning(
                f"This audio/video file ({path.suffix}) is not supported. Supported formats: {MEDIA_FORMATS}"
            )
            return None

        # Convert to wav if needed
        if path.suffix == ".wav":
            audio_path_wav = path
        else:
            # Run blocking operations in a thread
            sound = await asyncio.to_thread(AudioSegment.from_file, file=path, format=path.suffix[1:])
            audio_path_wav = path.with_suffix(".wav")
            await asyncio.to_thread(sound.export, audio_path_wav, format="wav")

        content = await self.transcriber.transcribe(audio_path_wav)
        if path.suffix != ".wav":
            await asyncio.to_thread(os.remove, audio_path_wav)
        doc = Document(page_content=content, metadata=metadata)
        if save_markdown:
            self.save_content(content, str(file_path))
        return doc
