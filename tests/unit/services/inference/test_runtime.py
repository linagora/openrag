import threading
import time

from core.utils.logging import get_logger
from fast_langdetect import LangDetector
from fast_langdetect import infer as fast_langdetect_infer
from services.inference import runtime

logger = get_logger()


class _Detector:
    def __init__(self, outputs=None, error: Exception | None = None):
        self.outputs = outputs
        self.error = error

    def detect(self, text: str, k: int, threshold: float = 0.0):
        if self.error is not None:
            raise self.error
        return self.outputs


def test_detect_language_returns_none_for_blank_input():
    assert runtime.detect_language("   ") is None


def test_detect_language_returns_none_when_detector_fails(monkeypatch):
    monkeypatch.setattr(runtime, "_lang_detector", _Detector(error=RuntimeError("boom")))

    assert runtime.detect_language("hello") is None


def test_detector_failure_is_logged_once(monkeypatch):
    records: list[str] = []
    monkeypatch.setattr(runtime, "_detector_failure_logged", False)
    monkeypatch.setattr(runtime, "_lang_detector", _Detector(error=RuntimeError("download failed")))
    sink_id = logger.add(records.append, level="WARNING", format="{message}")
    try:
        assert runtime.detect_language("hello") is None
        assert runtime.detect_language("hello again") is None
    finally:
        logger.remove(sink_id)

    assert len(records) == 1
    assert "download failed" in records[0]


def test_detect_language_returns_none_for_empty_output(monkeypatch):
    monkeypatch.setattr(runtime, "_lang_detector", _Detector(outputs=[]))

    assert runtime.detect_language("hello") is None


def test_detect_language_returns_lang(monkeypatch):
    monkeypatch.setattr(runtime, "_lang_detector", _Detector(outputs=[{"lang": "en"}]))

    assert runtime.detect_language("hello") == "en"


def test_detect_language_rejects_results_below_the_requested_confidence(monkeypatch):
    class LowConfidenceDetector:
        def detect(self, _text: str, *, k: int, threshold: float = 0.0):
            return [] if threshold > 0.2 else [{"lang": "xx", "score": 0.2}]

    monkeypatch.setattr(runtime, "_lang_detector", LowConfidenceDetector())

    assert runtime.detect_language("ambiguous", min_confidence=0.8) is None


def test_detector_uses_the_configured_model(monkeypatch):
    class _Settings:
        class language_detection:
            model = "full"

    monkeypatch.setattr(runtime, "_lang_detector", None)
    monkeypatch.setattr(runtime, "load_config", lambda: _Settings())
    monkeypatch.setattr(LangDetector, "detect", lambda *a, **k: [])

    assert runtime._get_lang_detector().config.model == "full"
    assert runtime._get_lang_detector() is runtime._lang_detector


def test_detector_uses_the_library_cache_when_the_image_cache_is_missing(monkeypatch, tmp_path):
    class _Settings:
        class language_detection:
            model = "full"

    monkeypatch.setattr(runtime, "_lang_detector", None)
    monkeypatch.setattr(runtime, "load_config", lambda: _Settings())
    monkeypatch.setattr(LangDetector, "detect", lambda *a, **k: [])
    monkeypatch.setattr(runtime, "_LANG_DETECT_CACHE_DIR", str(tmp_path / "missing"))
    assert runtime._get_lang_detector().config.cache_dir == fast_langdetect_infer.CACHE_DIRECTORY

    monkeypatch.setattr(runtime, "_lang_detector", None)
    monkeypatch.setattr(runtime, "_LANG_DETECT_CACHE_DIR", str(tmp_path))
    assert runtime._get_lang_detector().config.cache_dir == str(tmp_path)


def test_lite_model_detects_without_network_access(monkeypatch):
    import socket

    def no_network(*_args, **_kwargs):
        raise OSError("network access attempted")

    class _Settings:
        class language_detection:
            model = "lite"

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(runtime, "_lang_detector", None)
    monkeypatch.setattr(runtime, "load_config", lambda: _Settings())

    assert runtime.detect_language("Bonjour, je voudrais savoir comment fonctionne ce service.") == "fr"


class _Settings:
    class language_detection:
        model = "full"


def _fake_models(monkeypatch, *, full_error: Exception | None, lite_error: Exception | None = None, delay=0.0):
    """Replace the detector builder; return the list of models whose load was attempted."""
    attempts: list[str] = []

    def build(model):
        class _Model:
            def detect(self, text, k, threshold=0.0):
                if text == "warm up":
                    attempts.append(model)
                    time.sleep(delay)
                    error = full_error if model == "full" else lite_error
                    if error is not None:
                        raise error
                return [{"lang": model}]

        return _Model()

    monkeypatch.setattr(runtime, "_build_lang_detector", build)
    monkeypatch.setattr(runtime, "load_config", lambda: _Settings())
    monkeypatch.setattr(runtime, "_lang_detector", None)
    monkeypatch.setattr(runtime, "_lang_detector_unavailable", False)
    monkeypatch.setattr(runtime, "_detector_failure_logged", False)
    return attempts


def test_a_failed_model_load_is_attempted_once_then_lite_is_used(monkeypatch):
    attempts = _fake_models(monkeypatch, full_error=fast_langdetect_infer.FastLangdetectError("no route"))
    records: list[str] = []
    sink_id = logger.add(records.append, level="WARNING", format="{message}")
    try:
        results = [runtime.detect_language("hello") for _ in range(3)]
    finally:
        logger.remove(sink_id)

    assert results == ["lite"] * 3
    assert attempts == ["full", "lite"]
    assert len(records) == 1
    assert "LANGDETECT_MODEL" in records[0] and "no route" in records[0]


def test_detection_gives_up_at_once_when_lite_fails_too(monkeypatch):
    error = fast_langdetect_infer.FastLangdetectError("broken")
    attempts = _fake_models(monkeypatch, full_error=error, lite_error=error)

    assert [runtime.detect_language("hello") for _ in range(3)] == [None] * 3
    assert attempts == ["full", "lite"]


def test_concurrent_first_calls_load_the_model_once(monkeypatch):
    attempts = _fake_models(monkeypatch, full_error=None, delay=0.2)
    results: list = []
    threads = [threading.Thread(target=lambda: results.append(runtime.detect_language("hello"))) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert attempts == ["full"]
    assert results == ["full"] * 5


def test_a_non_library_load_error_falls_back_to_lite_once(monkeypatch):
    attempts = _fake_models(monkeypatch, full_error=MemoryError())

    assert [runtime.detect_language("hello") for _ in range(3)] == ["lite"] * 3
    assert attempts == ["full", "lite"]


def test_lite_failing_with_any_error_disables_detection_and_says_so_once(monkeypatch):
    attempts = _fake_models(monkeypatch, full_error=OSError("no cache"), lite_error=RuntimeError("corrupt"))
    records: list[str] = []
    sink_id = logger.add(records.append, level="ERROR", format="{message}")
    try:
        results = [runtime.detect_language("hello") for _ in range(3)]
    finally:
        logger.remove(sink_id)

    assert results == [None] * 3
    assert attempts == ["full", "lite"]
    assert runtime._lang_detector_unavailable is True
    assert len(records) == 1
    assert "disabled" in records[0] and "corrupt" in records[0]


async def test_detect_language_off_loop_runs_on_the_dedicated_thread(monkeypatch):
    class _Named:
        def detect(self, text, k, threshold=0.0):
            return [{"lang": threading.current_thread().name}]

    monkeypatch.setattr(runtime, "_lang_detector", _Named())

    lang = await runtime.detect_language_off_loop("hello")

    assert lang.startswith("lang-detect")
