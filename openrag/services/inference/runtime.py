"""Runtime inference helpers that depend on infrastructure services."""

from core.config import load_config
from fast_langdetect import LangDetectConfig, LangDetector
from services.inference.distributed_semaphore import DistributedSemaphore

_LANG_DETECT_CACHE_DIR = "/app/model_weights/"
_lang_detector = LangDetector(
    config=LangDetectConfig(
        max_input_length=1024,
        model="auto",
        cache_dir=_LANG_DETECT_CACHE_DIR,
    )
)


def detect_language(text: str):
    """Detect the primary language of ``text``."""
    outputs = _lang_detector.detect(text, k=1)
    return outputs[0].get("lang")


def get_llm_semaphore() -> DistributedSemaphore:
    """Return the distributed semaphore for LLM calls."""
    config = load_config()
    return DistributedSemaphore(
        name="llmSemaphore",
        max_concurrent_ops=config.semaphore.llm_semaphore,
    )


def get_vlm_semaphore() -> DistributedSemaphore:
    """Return the distributed semaphore for VLM calls."""
    config = load_config()
    return DistributedSemaphore(
        name="vlmSemaphore",
        max_concurrent_ops=config.semaphore.vlm_semaphore,
    )


def get_audio_semaphore() -> DistributedSemaphore:
    """Return the distributed semaphore for audio transcription calls."""
    config = load_config()
    return DistributedSemaphore(
        name="audioSemaphore",
        max_concurrent_ops=config.loader.transcriber.max_concurrent_chunks,
    )
