from services.inference import runtime


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
