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
    normalized = text.strip() if isinstance(text, str) else ""
    if not normalized:
        return None
    try:
        outputs = _lang_detector.detect(normalized, k=1)
    except Exception:
        return None
    if not outputs:
        return None
    first = outputs[0]
    return first.get("lang") if isinstance(first, dict) else None


def acquire_timeout_for(config, per_call_timeout: float) -> float:
    """Bound on waiting for a permit, as a multiple of one call's own timeout.

    Keeps every duration configured in exactly one place: a deployment tunes the
    per-call timeouts it already has (``vlm.timeout``, ``llm.timeout``,
    ``loader.transcriber.timeout``) and this scales them into a wait bound, so
    nothing here carries a hardcoded number of seconds.
    """
    return config.semaphore.acquire_timeout_factor * float(per_call_timeout)


def get_llm_semaphore() -> DistributedSemaphore:
    """Return the distributed semaphore for LLM calls."""
    config = load_config()
    return DistributedSemaphore(
        name="llmSemaphore",
        max_concurrent_ops=config.semaphore.llm_semaphore,
        acquire_timeout=acquire_timeout_for(config, config.llm.timeout),
    )


def get_vlm_semaphore() -> DistributedSemaphore:
    """Return the distributed semaphore for VLM calls."""
    config = load_config()
    return DistributedSemaphore(
        name="vlmSemaphore",
        max_concurrent_ops=config.semaphore.vlm_semaphore,
        acquire_timeout=acquire_timeout_for(config, config.vlm.timeout),
    )


def get_audio_semaphore() -> DistributedSemaphore:
    """Return the distributed semaphore for audio transcription calls."""
    config = load_config()
    return DistributedSemaphore(
        name="audioSemaphore",
        max_concurrent_ops=config.loader.transcriber.max_concurrent_chunks,
        acquire_timeout=acquire_timeout_for(config, config.loader.transcriber.timeout),
    )
