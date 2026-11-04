from core.utils.logging import get_logger
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

    assert runtime._get_lang_detector().config.model == "full"
    assert runtime._get_lang_detector() is runtime._lang_detector


def test_detector_uses_the_library_cache_when_the_image_cache_is_missing(monkeypatch, tmp_path):
    class _Settings:
        class language_detection:
            model = "full"

    monkeypatch.setattr(runtime, "_lang_detector", None)
    monkeypatch.setattr(runtime, "load_config", lambda: _Settings())
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
