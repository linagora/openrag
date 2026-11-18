"""Runtime inference helpers that depend on infrastructure services."""

import asyncio
import functools
import os
import threading
from concurrent.futures import ThreadPoolExecutor

from core.config import load_config
from core.utils.logging import get_logger
from fast_langdetect import LangDetectConfig, LangDetector
from services.inference.distributed_semaphore import DistributedSemaphore

logger = get_logger()

_LANG_DETECT_CACHE_DIR = "/app/model_weights/"
# Built on first use, with the model from ``language_detection.model``.
_lang_detector: LangDetector | None = None
# Detection runs in worker threads: only one of them builds the detector and
# attempts the first model load, the others wait for its outcome.
_lang_detector_lock = threading.Lock()
# Set once the bundled model fails too: detection then answers None at once.
_lang_detector_unavailable = False
# Callers fall back to a default language, so a broken detector is otherwise
# silent: report its first failure once per process.
_detector_failure_logged = False
# Detections wait here for the first model load, which can block for minutes:
# a dedicated thread keeps them off the event loop's default executor, shared
# with the other CPU-bound stages.
_lang_detect_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lang-detect")


def _build_lang_detector(model: str) -> LangDetector:
    # fast-langdetect refuses a cache_dir that doesn't exist, so outside the
    # images (which create /app/model_weights) let it use its own temp cache.
    return LangDetector(
        config=LangDetectConfig(
            max_input_length=1024,
            model=model,
            cache_dir=_LANG_DETECT_CACHE_DIR if os.path.isdir(_LANG_DETECT_CACHE_DIR) else None,
        )
    )


def _load_lang_detector() -> None:
    # ``full`` and ``auto`` download the full model on first use and cache it
    # only once the download succeeds: without network access, every attempt
    # would retry it and block for minutes. A failed load is paid once per
    # process, then the bundled ``lite`` model (shipped inside the
    # fast-langdetect package) takes over. Any exception counts as a failed
    # load (a download error, but also MemoryError or an unreadable cache).
    global _lang_detector, _lang_detector_unavailable
    model = load_config().language_detection.model
    try:
        detector = _build_lang_detector(model)
        # Loads the model now rather than on the first detection.
        detector.detect("warm up", k=1)
    except Exception as exc:
        if model != "lite":
            logger.warning(
                "Language detection model '{}' (LANGDETECT_MODEL / language_detection.model) could not be loaded, "
                "using the bundled 'lite' model instead: {}",
                model,
                exc,
            )
            try:
                detector = _build_lang_detector("lite")
                detector.detect("warm up", k=1)
            except Exception as lite_exc:
                exc = lite_exc
            else:
                _lang_detector = detector
                return
        _lang_detector_unavailable = True
        logger.error(
            "Language detection is disabled for this process: the bundled 'lite' model could not be loaded. "
            "Files fall back to the default language: {}",
            exc,
        )
        return
    _lang_detector = detector


def _get_lang_detector() -> LangDetector | None:
    if _lang_detector is not None:
        return _lang_detector
    if _lang_detector_unavailable:
        return None
    with _lang_detector_lock:
        if _lang_detector_unavailable:
            return None
        if _lang_detector is None:
            _load_lang_detector()
    return _lang_detector


def detect_language(text: str, *, min_confidence: float = 0.0):
    """Detect the primary language of ``text`` above an optional confidence floor."""
    normalized = text.strip() if isinstance(text, str) else ""
    if not normalized:
        return None
    try:
        detector = _get_lang_detector()
        if detector is None:
            return None
        outputs = detector.detect(normalized, k=1, threshold=min_confidence)
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


async def detect_language_off_loop(text: str, *, min_confidence: float = 0.0):
    """Run ``detect_language`` on the dedicated detection thread."""
    return await asyncio.get_running_loop().run_in_executor(
        _lang_detect_executor, functools.partial(detect_language, text, min_confidence=min_confidence)
    )


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
