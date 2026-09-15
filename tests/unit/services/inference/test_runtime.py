from types import SimpleNamespace

from services.inference import runtime


class _Detector:
    def __init__(self, outputs=None, error: Exception | None = None):
        self.outputs = outputs
        self.error = error

    def detect(self, text: str, k: int):
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


class TestAcquireTimeoutDerivation:
    """The permit-wait bound carries no duration of its own: it scales whatever
    per-call timeout that backend is already configured with, so a deployment
    tunes seconds in one place.
    """

    def test_scales_the_per_call_timeout_by_the_configured_factor(self):
        from services.inference.runtime import acquire_timeout_for

        config = SimpleNamespace(semaphore=SimpleNamespace(acquire_timeout_factor=4.0))

        assert acquire_timeout_for(config, 60) == 240.0
        assert acquire_timeout_for(config, 120.0) == 480.0

    def test_each_gate_derives_from_its_own_backend(self):
        from core.config import load_config
        from services.inference.runtime import acquire_timeout_for, get_llm_semaphore, get_vlm_semaphore

        config = load_config()

        assert get_llm_semaphore()._acquire_timeout == acquire_timeout_for(config, config.llm.timeout)
        assert get_vlm_semaphore()._acquire_timeout == acquire_timeout_for(config, config.vlm.timeout)
