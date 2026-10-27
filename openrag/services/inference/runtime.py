"""Runtime inference helpers that depend on infrastructure services."""

import os

from core.config import load_config
from core.utils.logging import get_logger
from fast_langdetect import LangDetectConfig, LangDetector
from services.inference.distributed_semaphore import DistributedSemaphore

logger = get_logger()

_LANG_DETECT_CACHE_DIR = "/app/model_weights/"
# Built on first use, with the model from ``language_detection.model``.
_lang_detector: LangDetector | None = None
# Callers fall back to a default language, so a broken detector is otherwise
# silent: report its first failure once per process.
_detector_failure_logged = False


def _get_lang_detector() -> LangDetector:
    # ``full`` and ``auto`` download the full model on first use and cache it
    # only once the download succeeds: without network access, every call
    # retries it. ``lite`` ships inside the fast-langdetect package.
    # fast-langdetect refuses a cache_dir that doesn't exist, so outside the
    # images (which create /app/model_weights) let it use its own temp cache.
    global _lang_detector
    if _lang_detector is None:
        _lang_detector = LangDetector(
            config=LangDetectConfig(
                max_input_length=1024,
                model=load_config().language_detection.model,
                cache_dir=_LANG_DETECT_CACHE_DIR if os.path.isdir(_LANG_DETECT_CACHE_DIR) else None,
            )
        )
    return _lang_detector


def detect_language(text: str, *, min_confidence: float = 0.0):
    """Detect the primary language of ``text`` above an optional confidence floor."""
    normalized = text.strip() if isinstance(text, str) else ""
    if not normalized:
        return None
    try:
        outputs = _get_lang_detector().detect(normalized, k=1, threshold=min_confidence)
    except Exception as exc:
        global _detector_failure_logged
        if not _detector_failure_logged:
            _detector_failure_logged = True
            logger.warning("Language detection failed, falling back to the default language: {}", exc)
        return None
    if not outputs:
        return None
    first = outputs[0]
    return first.get("lang") if isinstance(first, dict) else None


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
