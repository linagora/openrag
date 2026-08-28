"""Unit tests for :class:`OpenAIAudioClient`.

``pydub`` is shimmed at import time via ``sys.modules`` so the test
runs on Python 3.13 (where ``audioop`` was dropped from stdlib and
plain ``import pydub`` fails). The mock is good enough for the control
flow we exercise — neither real audio decoding nor a real OpenAI
client is needed.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---- shim pydub before importing openai_audio ------------------------------

if "pydub" not in sys.modules:
    try:
        import pydub  # noqa: E402,F401  — prefer the real library when it imports cleanly
    except (ImportError, ModuleNotFoundError):
        fake_pydub = types.ModuleType("pydub")
        fake_pydub.AudioSegment = MagicMock()  # type: ignore[attr-defined]
        sys.modules["pydub"] = fake_pydub

from core.config.model_endpoints import ModelEndpointConfig  # noqa: E402
from core.models.document import Document, DocumentType  # noqa: E402
from services.inference.parsers.openai_audio import OpenAIAudioClient  # noqa: E402

# ---- shared fixtures -------------------------------------------------------


@pytest.fixture
def mock_openai_client():
    """Build an ``AsyncOpenAI``-shaped mock with an awaitable ``audio.transcriptions.create``."""
    fake = MagicMock()
    fake.audio = MagicMock()
    fake.audio.transcriptions = MagicMock()
    fake.audio.transcriptions.create = AsyncMock()
    return fake


def _client(mock_openai_client, **overrides) -> OpenAIAudioClient:
    defaults = {"base_url": "http://x", "api_key": "k", "model": "whisper-mock"}
    client = OpenAIAudioClient(**{**defaults, **overrides})
    # Constructor stores config only; swap in our mock before any call.
    client._client = mock_openai_client
    return client


def _audio_doc(raw: bytes = b"audio-bytes", filename: str = "x.mp3") -> Document:
    return Document(filename=filename, content_type=DocumentType.AUDIO, raw_bytes=raw)


# ---- _prepare_upload -------------------------------------------------------


class TestPrepareUpload:
    @pytest.mark.asyncio
    async def test_direct_upload_skips_conversion(self, mock_openai_client):
        client = _client(mock_openai_client)
        path = Path("/tmp/audio.mp3")
        upload, cleanup = await client._prepare_upload(path)
        assert upload == path
        assert cleanup is False

    @pytest.mark.asyncio
    async def test_unsupported_suffix_falls_back_to_wav(self, mock_openai_client, monkeypatch):
        from services.inference.parsers import openai_audio as mod

        sound = MagicMock()
        sound.__len__ = MagicMock(return_value=1500)
        sound.export = MagicMock()
        from_file = MagicMock(return_value=sound)
        monkeypatch.setattr(mod.AudioSegment, "from_file", from_file)

        client = _client(mock_openai_client)
        path = Path("/tmp/audio.flac")
        upload, cleanup = await client._prepare_upload(path)

        assert upload == path.with_suffix(".wav")
        assert cleanup is True
        from_file.assert_called_once_with(path)
        sound.export.assert_called_once()
        assert sound.export.call_args.kwargs == {"format": "wav"}


# ---- parse() ---------------------------------------------------------------


class TestParse:
    @pytest.mark.asyncio
    async def test_empty_raw_bytes_returns_empty(self, mock_openai_client):
        result = await _client(mock_openai_client).parse(_audio_doc(raw=b""))
        assert result.text_blocks == [] and result.page_count == 0

    @pytest.mark.asyncio
    async def test_returns_text_block_on_success(self, mock_openai_client):
        mock_openai_client.audio.transcriptions.create.return_value = MagicMock(text=" hello world ")
        result = await _client(mock_openai_client).parse(_audio_doc())

        assert len(result.text_blocks) == 1
        assert result.text_blocks[0].text == "hello world"
        assert result.text_blocks[0].page_number == 1
        assert result.page_count == 1
        mock_openai_client.audio.transcriptions.create.assert_awaited_once()
        kwargs = mock_openai_client.audio.transcriptions.create.await_args.kwargs
        assert kwargs["model"] == "whisper-mock"
        assert "language" not in kwargs
        assert "prompt" not in kwargs

    @pytest.mark.asyncio
    async def test_transcription_prompt_is_forwarded(self, mock_openai_client):
        mock_openai_client.audio.transcriptions.create.return_value = MagicMock(text="hello")
        prompt = "Transcribe verbatim and preserve the product name: OpenRAG."
        resolver = AsyncMock(return_value=f"  {prompt}  ")

        await _client(mock_openai_client, transcription_prompt_resolver=resolver).parse(_audio_doc())

        resolver.assert_awaited_once()
        kwargs = mock_openai_client.audio.transcriptions.create.await_args.kwargs
        assert kwargs["prompt"] == prompt

    @pytest.mark.asyncio
    async def test_blank_managed_transcription_prompt_uses_endpoint_default(self, mock_openai_client):
        mock_openai_client.audio.transcriptions.create.return_value = MagicMock(text="hello")
        resolver = AsyncMock(return_value="   ")

        await _client(mock_openai_client, transcription_prompt_resolver=resolver).parse(_audio_doc())

        resolver.assert_awaited_once()
        kwargs = mock_openai_client.audio.transcriptions.create.await_args.kwargs
        assert "prompt" not in kwargs

    @pytest.mark.asyncio
    async def test_transcription_prompt_lookup_failure_uses_endpoint_default(self, mock_openai_client):
        mock_openai_client.audio.transcriptions.create.return_value = MagicMock(text="hello")
        resolver = AsyncMock(side_effect=RuntimeError("prompt database unavailable"))

        result = await _client(mock_openai_client, transcription_prompt_resolver=resolver).parse(_audio_doc())

        kwargs = mock_openai_client.audio.transcriptions.create.await_args.kwargs
        assert "prompt" not in kwargs
        assert result.text_blocks[0].text == "hello"

    @pytest.mark.asyncio
    async def test_empty_transcript_yields_no_text_block(self, mock_openai_client):
        mock_openai_client.audio.transcriptions.create.return_value = MagicMock(text="   ")
        result = await _client(mock_openai_client).parse(_audio_doc())
        assert result.text_blocks == [] and result.page_count == 0

    @pytest.mark.asyncio
    async def test_language_detector_result_forwarded(self, mock_openai_client):
        mock_openai_client.audio.transcriptions.create.return_value = MagicMock(text="bonjour")
        detector = AsyncMock(return_value="fr")
        result = await _client(mock_openai_client, language_detector=detector).parse(_audio_doc())

        detector.assert_awaited_once()
        kwargs = mock_openai_client.audio.transcriptions.create.await_args.kwargs
        assert kwargs["language"] == "fr"
        assert result.text_blocks[0].text == "bonjour"

    @pytest.mark.asyncio
    async def test_language_detector_failure_is_swallowed(self, mock_openai_client):
        mock_openai_client.audio.transcriptions.create.return_value = MagicMock(text="ok")
        detector = AsyncMock(side_effect=RuntimeError("detector down"))
        result = await _client(mock_openai_client, language_detector=detector).parse(_audio_doc())

        # Transcription proceeds without ``language`` and the call still succeeds.
        kwargs = mock_openai_client.audio.transcriptions.create.await_args.kwargs
        assert "language" not in kwargs
        assert result.text_blocks[0].text == "ok"

    @pytest.mark.asyncio
    async def test_stt_language_hint_overrides_whisper_language_detector(self, mock_openai_client):
        mock_openai_client.audio.transcriptions.create.return_value = MagicMock(text="bonjour")
        detector = AsyncMock(return_value="en")
        endpoint = ModelEndpointConfig(
            endpoint="http://x",
            model_name="moss-transcribe-diarize",
            batch_size=3,
            timeout=120,
            extra={"api_key": "k", "language": "fr"},
        )

        client = _client(
            mock_openai_client,
            language_detector=detector,
            transcription_endpoint_resolver=lambda: endpoint,
        )
        await client.parse(_audio_doc())

        detector.assert_not_awaited()
        kwargs = mock_openai_client.audio.transcriptions.create.await_args.kwargs
        assert kwargs["model"] == "moss-transcribe-diarize"
        assert kwargs["language"] == "fr"
        assert client._semaphore_for_endpoint(endpoint)._value == 3

    @pytest.mark.asyncio
    async def test_stt_request_extra_is_forwarded_without_connection_metadata(self, mock_openai_client):
        mock_openai_client.audio.transcriptions.create.return_value = MagicMock(text="bonjour")
        endpoint = ModelEndpointConfig(
            endpoint="http://x",
            model_name="moss-transcribe-diarize",
            batch_size=1,
            timeout=120,
            extra={
                "api_key": "k",
                "language": "fr",
                "managed_by": "env",
                "implementation": "vllm",
                "file": "must-not-override-upload",
                "model": "must-not-override-endpoint",
                "prompt": "must-not-override-managed-prompt",
                "stream": True,
                "temperature": 0,
                "response_format": "json",
                "max_completion_tokens": 8192,
            },
        )
        client = _client(mock_openai_client, transcription_endpoint_resolver=lambda: endpoint)

        await client.parse(_audio_doc())

        kwargs = mock_openai_client.audio.transcriptions.create.await_args.kwargs
        assert kwargs["language"] == "fr"
        assert kwargs["extra_body"] == {
            "temperature": 0,
            "response_format": "json",
            "max_completion_tokens": 8192,
        }

    @pytest.mark.asyncio
    @pytest.mark.parametrize("extra", [{}, {"api_key": ""}, {"api_key": "   "}])
    async def test_stt_endpoint_without_key_reuses_fallback_client(self, mock_openai_client, monkeypatch, extra):
        from services.inference.parsers import openai_audio as module

        mock_openai_client.audio.transcriptions.create.return_value = MagicMock(text="transcribed")
        endpoint = ModelEndpointConfig(
            endpoint="http://x",
            model_name="moss-transcribe-diarize",
            batch_size=1,
            timeout=120,
            extra=extra,
        )

        client = _client(
            mock_openai_client,
            api_key="EMPTY",
            transcription_endpoint_resolver=lambda: endpoint,
        )
        unexpected_client = MagicMock(side_effect=AssertionError("a fallback endpoint must not create a new client"))
        monkeypatch.setattr(module, "AsyncOpenAI", unexpected_client)

        result = await client.parse(_audio_doc())

        assert result.text_blocks[0].text == "transcribed"
        unexpected_client.assert_not_called()
        assert mock_openai_client.audio.transcriptions.create.await_args.kwargs["model"] == "moss-transcribe-diarize"

    @pytest.mark.asyncio
    async def test_stt_endpoint_resolver_replaces_connection_and_model(self, monkeypatch):
        from services.inference.parsers import openai_audio as module

        created: list[tuple[dict[str, object], MagicMock]] = []

        def make_openai_client(**kwargs):
            client = MagicMock()
            client.audio = MagicMock()
            client.audio.transcriptions = MagicMock()
            client.audio.transcriptions.create = AsyncMock(return_value=MagicMock(text="transcribed"))
            client.close = AsyncMock()
            created.append((kwargs, client))
            return client

        monkeypatch.setattr(module, "AsyncOpenAI", make_openai_client)
        endpoint = ModelEndpointConfig(
            endpoint="http://moss:8000/v1",
            model_name="moss-transcribe-diarize",
            batch_size=1,
            timeout=900,
            extra={"api_key": "endpoint-key"},
        )

        async def resolve_endpoint():
            return endpoint

        client = OpenAIAudioClient(
            base_url="http://whisper:8000/v1",
            api_key="legacy-key",
            model="whisper-model",
            transcription_endpoint_resolver=resolve_endpoint,
        )

        result = await client.parse(_audio_doc())

        assert result.text_blocks[0].text == "transcribed"
        assert created[1][0] == {
            "base_url": "http://moss:8000/v1",
            "api_key": "endpoint-key",
            "timeout": 900,
        }
        request = created[1][1].audio.transcriptions.create.await_args.kwargs
        assert request["model"] == "moss-transcribe-diarize"
        created[1][1].close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_transcribe_exception_propagates(self, mock_openai_client):
        mock_openai_client.audio.transcriptions.create.side_effect = RuntimeError("api down")
        with pytest.raises(RuntimeError, match="api down"):
            await _client(mock_openai_client).parse(_audio_doc())


def test_supported_types(mock_openai_client):
    types_ = _client(mock_openai_client).supported_types()
    assert DocumentType.AUDIO.value in types_
    assert DocumentType.VIDEO.value in types_
