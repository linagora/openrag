"""Unit tests for :class:`OpenAIAudioClient`.

``pydub`` is shimmed at import time via ``sys.modules`` so the test
runs on Python 3.13 (where ``audioop`` was dropped from stdlib and
plain ``import pydub`` fails). The mock is good enough for the control
flow we exercise — neither real audio decoding nor a real OpenAI
client is needed.
"""

from __future__ import annotations

import asyncio
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

from core.config.model_endpoints import (  # noqa: E402
    MOSS_SPEAKER_AWARE_KEY,
    ModelEndpointConfig,
)
from core.models.document import Document, DocumentType  # noqa: E402
from core.utils.exceptions import NotFoundError  # noqa: E402
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


async def _wait_for_endpoint_leases(
    client: OpenAIAudioClient,
    key: tuple[str, str, str | None],
    expected: int,
) -> None:
    """Wait until a request has registered as active or queued."""
    for _ in range(10):
        entry = client._endpoint_limiters.get(key)
        if entry is not None and entry.leases == expected:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"Expected {expected} leases for {key}")


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
    async def test_selected_stt_endpoint_resolution_error_fails_transcription(self, mock_openai_client):
        async def resolve_endpoint():
            raise KeyError("Unknown STT endpoint 'retired-moss'")

        client = _client(mock_openai_client, transcription_endpoint_resolver=resolve_endpoint)

        with pytest.raises(KeyError, match="retired-moss"):
            await client.parse(_audio_doc())

        mock_openai_client.audio.transcriptions.create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_selected_transcription_prompt_resolution_error_fails_transcription(self, mock_openai_client):
        async def resolve_prompt():
            raise NotFoundError("Selected ASR prompt 'retired-asr' no longer exists")

        client = _client(mock_openai_client, transcription_prompt_resolver=resolve_prompt)

        with pytest.raises(NotFoundError, match="retired-asr"):
            await client.parse(_audio_doc())

        mock_openai_client.audio.transcriptions.create.assert_not_awaited()

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
                "max_llm_context_size": 8192,
                "max_output_tokens": 1024,
                MOSS_SPEAKER_AWARE_KEY: True,
                "temperature": 0,
                "response_format": "json",
                "max_completion_tokens": 8192,
            },
        )
        client = _client(mock_openai_client, transcription_endpoint_resolver=lambda: endpoint)

        await client.parse(_audio_doc())

        kwargs = mock_openai_client.audio.transcriptions.create.await_args.kwargs
        assert kwargs["language"] == "fr"
        assert kwargs["response_format"] == "json"
        assert kwargs["extra_body"] == {
            "temperature": 0,
            "max_completion_tokens": 8192,
        }

    @pytest.mark.asyncio
    async def test_moss_speaker_aware_output_hides_a_single_speaker(self, mock_openai_client):
        mock_openai_client.audio.transcriptions.create.return_value = MagicMock(
            text="[1.12-2.32][S1] Hello everyone.[2.68-4.32][S01] This week."
        )
        endpoint = ModelEndpointConfig(
            endpoint="http://x",
            model_name="moss-transcribe-diarize",
            batch_size=1,
            timeout=120,
            extra={
                "api_key": "k",
                MOSS_SPEAKER_AWARE_KEY: True,
            },
        )

        result = await _client(mock_openai_client, transcription_endpoint_resolver=lambda: endpoint).parse(_audio_doc())

        assert result.text_blocks[0].text == "Hello everyone.\nThis week."

    @pytest.mark.asyncio
    async def test_moss_output_stays_raw_without_speaker_aware_normalization(self, mock_openai_client):
        transcript = "[1.12-2.32][S01] Hello everyone."
        mock_openai_client.audio.transcriptions.create.return_value = MagicMock(text=transcript)
        endpoint = ModelEndpointConfig(
            endpoint="http://x",
            model_name="moss-transcribe-diarize",
            batch_size=1,
            timeout=120,
            extra={"api_key": "k"},
        )

        result = await _client(mock_openai_client, transcription_endpoint_resolver=lambda: endpoint).parse(_audio_doc())

        assert result.text_blocks[0].text == transcript

    @pytest.mark.asyncio
    async def test_stt_endpoint_limiter_reuses_active_entry_across_a_b_a_switch(self, mock_openai_client):
        client = _client(mock_openai_client)
        endpoint_a = ModelEndpointConfig(
            endpoint="http://moss-a:8000/v1",
            model_name="moss",
            batch_size=1,
            timeout=120,
        )
        endpoint_b = endpoint_a.model_copy(update={"endpoint": "http://moss-b:8000/v1"})
        endpoint_a_alias = endpoint_a.model_copy(
            update={"endpoint": "http://moss-a:8000/v1/", "model_name": "  moss  "}
        )
        first_entered = asyncio.Event()
        second_entered = asyncio.Event()
        release_first = asyncio.Event()

        async def first_request() -> None:
            async with client._transcription_slot(endpoint_a):
                first_entered.set()
                await release_first.wait()

        async def second_request() -> None:
            async with client._transcription_slot(endpoint_a_alias):
                second_entered.set()

        first = asyncio.create_task(first_request())
        await asyncio.wait_for(first_entered.wait(), timeout=0.5)
        async with client._transcription_slot(endpoint_b):
            pass

        second = asyncio.create_task(second_request())
        await _wait_for_endpoint_leases(client, ("http://moss-a:8000/v1", "moss", None), 2)
        assert not second_entered.is_set()

        release_first.set()
        await asyncio.gather(first, second)
        assert second_entered.is_set()

    @pytest.mark.asyncio
    async def test_stt_endpoint_limiter_applies_a_lowered_limit_to_active_work(self, mock_openai_client):
        client = _client(mock_openai_client)
        endpoint = ModelEndpointConfig(
            name="moss",
            endpoint="http://moss:8000/v1",
            model_name="moss",
            batch_size=2,
            timeout=120,
        )

        first_entered = asyncio.Event()
        second_entered = asyncio.Event()
        third_entered = asyncio.Event()
        release_first = asyncio.Event()
        release_second = asyncio.Event()

        async def hold_slot(entered: asyncio.Event, release: asyncio.Event, config: ModelEndpointConfig) -> None:
            async with client._transcription_slot(config):
                entered.set()
                await release.wait()

        first = asyncio.create_task(hold_slot(first_entered, release_first, endpoint))
        second = asyncio.create_task(hold_slot(second_entered, release_second, endpoint))
        await asyncio.wait_for(first_entered.wait(), timeout=0.5)
        await asyncio.wait_for(second_entered.wait(), timeout=0.5)

        lowered_endpoint = endpoint.model_copy(update={"batch_size": 1})
        third = asyncio.create_task(hold_slot(third_entered, asyncio.Event(), lowered_endpoint))
        await _wait_for_endpoint_leases(client, ("http://moss:8000/v1", "moss", "moss"), 3)
        assert not third_entered.is_set()

        release_first.set()
        await first
        await asyncio.sleep(0)
        assert not third_entered.is_set()

        release_second.set()
        await second
        await asyncio.wait_for(third_entered.wait(), timeout=0.5)
        third.cancel()
        with pytest.raises(asyncio.CancelledError):
            await third

    @pytest.mark.asyncio
    async def test_stt_endpoint_limiter_releases_a_cancelled_queued_lease(self, mock_openai_client):
        client = _client(mock_openai_client)
        endpoint_a = ModelEndpointConfig(
            endpoint="http://moss-a:8000/v1",
            model_name="moss",
            batch_size=1,
            timeout=120,
        )
        endpoint_b = endpoint_a.model_copy(update={"endpoint": "http://moss-b:8000/v1"})
        first_entered = asyncio.Event()
        release_first = asyncio.Event()

        async def hold_first() -> None:
            async with client._transcription_slot(endpoint_a):
                first_entered.set()
                await release_first.wait()

        async def queue_second() -> None:
            async with client._transcription_slot(endpoint_a):
                pass

        first = asyncio.create_task(hold_first())
        await asyncio.wait_for(first_entered.wait(), timeout=0.5)
        queued = asyncio.create_task(queue_second())
        await _wait_for_endpoint_leases(client, ("http://moss-a:8000/v1", "moss", None), 2)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued

        async with client._transcription_slot(endpoint_b):
            pass
        release_first.set()
        await first

        assert set(client._endpoint_limiters) == {
            ("http://moss-a:8000/v1", "moss", None),
            ("http://moss-b:8000/v1", "moss", None),
        }

    @pytest.mark.asyncio
    async def test_stt_endpoint_capacity_wait_does_not_hold_the_registry_lock(self, mock_openai_client):
        client = _client(mock_openai_client)
        endpoint_a = ModelEndpointConfig(
            endpoint="http://moss-a:8000/v1",
            model_name="moss",
            batch_size=1,
            timeout=120,
        )
        endpoint_b = endpoint_a.model_copy(update={"endpoint": "http://moss-b:8000/v1"})
        first_entered = asyncio.Event()
        release_first = asyncio.Event()

        async def hold_first() -> None:
            async with client._transcription_slot(endpoint_a):
                first_entered.set()
                await release_first.wait()

        async def queue_second() -> None:
            async with client._transcription_slot(endpoint_a):
                pass

        first = asyncio.create_task(hold_first())
        await asyncio.wait_for(first_entered.wait(), timeout=0.5)
        queued = asyncio.create_task(queue_second())
        key_a = ("http://moss-a:8000/v1", "moss", None)
        await _wait_for_endpoint_leases(client, key_a, 2)

        async def use_endpoint_b() -> None:
            async with client._transcription_slot(endpoint_b):
                pass

        await asyncio.wait_for(use_endpoint_b(), timeout=0.5)

        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        release_first.set()
        await first

    @pytest.mark.asyncio
    async def test_stt_endpoint_limiter_releases_an_active_exception(self, mock_openai_client):
        client = _client(mock_openai_client)
        endpoint_a = ModelEndpointConfig(
            endpoint="http://moss-a:8000/v1",
            model_name="moss",
            batch_size=1,
            timeout=120,
        )
        endpoint_b = endpoint_a.model_copy(update={"endpoint": "http://moss-b:8000/v1"})

        with pytest.raises(RuntimeError, match="transcription failed"):
            async with client._transcription_slot(endpoint_a):
                raise RuntimeError("transcription failed")
        async with client._transcription_slot(endpoint_b):
            pass

        assert set(client._endpoint_limiters) == {
            ("http://moss-a:8000/v1", "moss", None),
            ("http://moss-b:8000/v1", "moss", None),
        }

    @pytest.mark.asyncio
    async def test_stt_endpoint_limiter_keeps_named_cache_across_fallback(self, mock_openai_client):
        client = _client(mock_openai_client, concurrency_limit=1)
        endpoint = ModelEndpointConfig(
            name="moss",
            endpoint="http://moss:8000/v1",
            model_name="moss",
            batch_size=1,
            timeout=120,
        )

        async with client._transcription_slot(None):
            pass
        async with client._transcription_slot(endpoint):
            pass
        async with client._transcription_slot(None):
            pass

        assert set(client._endpoint_limiters) == {("http://moss:8000/v1", "moss", "moss")}

    @pytest.mark.asyncio
    async def test_text_response_format_accepts_plain_string_response(self, mock_openai_client):
        mock_openai_client.audio.transcriptions.create.return_value = "bonjour"
        endpoint = ModelEndpointConfig(
            endpoint="http://x",
            model_name="moss-transcribe-diarize",
            batch_size=1,
            timeout=120,
            extra={"api_key": "k", "response_format": "text"},
        )

        result = await _client(mock_openai_client, transcription_endpoint_resolver=lambda: endpoint).parse(_audio_doc())

        assert result.text_blocks[0].text == "bonjour"
        kwargs = mock_openai_client.audio.transcriptions.create.await_args.kwargs
        assert kwargs["response_format"] == "text"
        assert "extra_body" not in kwargs

    @pytest.mark.asyncio
    async def test_malformed_saved_moss_setting_leaves_the_transcript_raw(self, mock_openai_client):
        transcript = "[1.12-2.32][S01] Hello everyone."
        mock_openai_client.audio.transcriptions.create.return_value = MagicMock(text=transcript)
        endpoint = ModelEndpointConfig(
            endpoint="http://x",
            model_name="moss-transcribe-diarize",
            batch_size=1,
            timeout=120,
            extra={"api_key": "k", MOSS_SPEAKER_AWARE_KEY: []},
        )

        result = await _client(mock_openai_client, transcription_endpoint_resolver=lambda: endpoint).parse(_audio_doc())

        assert result.text_blocks[0].text == transcript
        kwargs = mock_openai_client.audio.transcriptions.create.await_args.kwargs
        assert "extra_body" not in kwargs

    @pytest.mark.asyncio
    @pytest.mark.parametrize("extra", [{}, {"api_key": ""}, {"api_key": "   "}])
    async def test_stt_endpoint_without_key_does_not_receive_fallback_credential(self, monkeypatch, extra):
        from services.inference.parsers import openai_audio as module

        created: list[tuple[dict[str, object], MagicMock]] = []

        def make_openai_client(**kwargs):
            temporary_client = MagicMock()
            temporary_client.audio = MagicMock()
            temporary_client.audio.transcriptions = MagicMock()
            temporary_client.audio.transcriptions.create = AsyncMock(return_value=MagicMock(text="transcribed"))
            temporary_client.close = AsyncMock()
            created.append((kwargs, temporary_client))
            return temporary_client

        endpoint = ModelEndpointConfig(
            endpoint="http://x",
            model_name="moss-transcribe-diarize",
            batch_size=1,
            timeout=120,
            extra=extra,
        )

        client = _client(
            MagicMock(),
            api_key="legacy-key",
            transcription_endpoint_resolver=lambda: endpoint,
        )
        monkeypatch.setattr(module, "AsyncOpenAI", make_openai_client)

        result = await client.parse(_audio_doc())

        assert result.text_blocks[0].text == "transcribed"
        assert created[0][0] == {
            "base_url": "http://x",
            "api_key": "",
            "timeout": 120,
        }
        assert created[0][1].audio.transcriptions.create.await_args.kwargs["model"] == "moss-transcribe-diarize"
        created[0][1].close.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_keyless_stt_endpoint_does_not_receive_fallback_key_from_another_host(self, monkeypatch):
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

        endpoint = ModelEndpointConfig(
            endpoint="http://moss:8000/v1",
            model_name="moss-transcribe-diarize",
            batch_size=1,
            timeout=900,
            extra={},
        )
        client = _client(
            MagicMock(),
            base_url="http://whisper:8000/v1",
            api_key="legacy-key",
            transcription_endpoint_resolver=lambda: endpoint,
        )
        monkeypatch.setattr(module, "AsyncOpenAI", make_openai_client)

        result = await client.parse(_audio_doc())

        assert result.text_blocks[0].text == "transcribed"
        assert created[0][0] == {
            "base_url": "http://moss:8000/v1",
            "api_key": "",
            "timeout": 900,
        }
        created[0][1].close.assert_not_awaited()

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
        created[1][1].close.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_resolved_stt_endpoint_reuses_its_http_client(self, monkeypatch):
        from services.inference.parsers import openai_audio as module

        created: list[MagicMock] = []

        def make_openai_client(**_kwargs):
            client = MagicMock()
            client.audio = MagicMock()
            client.audio.transcriptions = MagicMock()
            client.audio.transcriptions.create = AsyncMock(return_value=MagicMock(text="transcribed"))
            client.close = AsyncMock()
            created.append(client)
            return client

        endpoint = ModelEndpointConfig(
            endpoint="http://moss:8000/v1",
            model_name="moss-transcribe-diarize",
            timeout=900,
            extra={"api_key": "endpoint-key"},
        )
        client = _client(
            MagicMock(),
            transcription_endpoint_resolver=lambda: endpoint,
        )
        monkeypatch.setattr(module, "AsyncOpenAI", make_openai_client)

        await client.parse(_audio_doc())
        await client.parse(_audio_doc())

        assert len(created) == 1
        assert created[0].audio.transcriptions.create.await_count == 2
        created[0].close.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stt_endpoint_caches_reuse_idle_entries_when_presets_alternate(self, monkeypatch):
        """Alternating partitions must not rebuild their endpoint clients or limiters."""
        from services.inference.parsers import openai_audio as module

        created: list[MagicMock] = []

        def make_openai_client(**_kwargs):
            client = MagicMock()
            client.close = AsyncMock()
            created.append(client)
            return client

        endpoint_a = ModelEndpointConfig(
            endpoint="http://moss-a:8000/v1",
            model_name="moss-a",
            batch_size=1,
            timeout=900,
            extra={"api_key": "key-a"},
        )
        endpoint_b = ModelEndpointConfig(
            endpoint="http://moss-b:8000/v1",
            model_name="moss-b",
            batch_size=1,
            timeout=900,
            extra={"api_key": "key-b"},
        )
        client = _client(MagicMock())
        monkeypatch.setattr(module, "AsyncOpenAI", make_openai_client)

        async def use(endpoint: ModelEndpointConfig) -> None:
            async with client._transcription_slot(endpoint):
                async with client._transcription_client(endpoint):
                    pass

        await use(endpoint_a)
        await use(endpoint_b)
        await use(endpoint_a)

        assert len(created) == 2
        assert set(client._endpoint_limiters) == {
            ("http://moss-a:8000/v1", "moss-a", None),
            ("http://moss-b:8000/v1", "moss-b", None),
        }
        assert set(client._endpoint_clients) == {
            ("http://moss-a:8000/v1", "key-a", 900.0, None),
            ("http://moss-b:8000/v1", "key-b", 900.0, None),
        }
        for created_client in created:
            created_client.close.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stt_endpoint_caches_keep_distinct_named_presets_on_the_same_server(self, monkeypatch):
        """Different registrations sharing a URL must not retire each other."""
        from services.inference.parsers import openai_audio as module

        created: list[MagicMock] = []

        def make_openai_client(**_kwargs):
            client = MagicMock()
            client.close = AsyncMock()
            created.append(client)
            return client

        endpoint_a = ModelEndpointConfig(
            name="moss-a",
            endpoint="http://shared-moss:8000/v1",
            model_name="moss",
            timeout=900,
            extra={"api_key": "key-a"},
        )
        endpoint_b = ModelEndpointConfig(
            name="moss-b",
            endpoint="http://shared-moss:8000/v1",
            model_name="moss",
            timeout=901,
            extra={"api_key": "key-b"},
        )
        client = _client(MagicMock())
        monkeypatch.setattr(module, "AsyncOpenAI", make_openai_client)

        async def use(endpoint: ModelEndpointConfig) -> None:
            async with client._transcription_client(endpoint):
                pass

        await use(endpoint_a)
        await use(endpoint_b)
        await use(endpoint_a)

        assert len(created) == 2
        for created_client in created:
            created_client.close.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stt_endpoint_cache_reactivates_a_reselected_configuration(self, monkeypatch):
        """A configuration reverting while its old request runs must keep its client."""
        from services.inference.parsers import openai_audio as module

        created: list[MagicMock] = []

        def make_openai_client(**_kwargs):
            client = MagicMock()
            client.close = AsyncMock()
            created.append(client)
            return client

        endpoint_a = ModelEndpointConfig(
            name="moss",
            endpoint="http://moss-a:8000/v1",
            model_name="moss",
            timeout=900,
            extra={"api_key": "key-a"},
        )
        endpoint_b = endpoint_a.model_copy(update={"endpoint": "http://moss-b:8000/v1", "extra": {"api_key": "key-b"}})
        client = _client(MagicMock())
        monkeypatch.setattr(module, "AsyncOpenAI", make_openai_client)
        first_entered = asyncio.Event()
        release_first = asyncio.Event()

        async def hold_first() -> None:
            async with client._transcription_client(endpoint_a):
                first_entered.set()
                await release_first.wait()

        async def use(endpoint: ModelEndpointConfig) -> None:
            async with client._transcription_client(endpoint):
                pass

        first = asyncio.create_task(hold_first())
        await first_entered.wait()
        await use(endpoint_b)
        await use(endpoint_a)
        release_first.set()
        await first

        assert len(created) == 2
        created[0].close.assert_not_awaited()
        assert ("http://moss-a:8000/v1", "key-a", 900.0, "moss") in client._endpoint_clients

    @pytest.mark.asyncio
    async def test_stt_endpoint_limiters_isolate_distinct_named_presets_on_the_same_route(self, mock_openai_client):
        client = _client(mock_openai_client)
        endpoint_a = ModelEndpointConfig(
            name="moss-a",
            endpoint="http://shared-moss:8000/v1",
            model_name="moss",
            batch_size=1,
        )
        endpoint_b = endpoint_a.model_copy(update={"name": "moss-b"})
        first_entered = asyncio.Event()
        second_entered = asyncio.Event()
        release = asyncio.Event()

        async def hold(endpoint: ModelEndpointConfig, entered: asyncio.Event) -> None:
            async with client._transcription_slot(endpoint):
                entered.set()
                await release.wait()

        first = asyncio.create_task(hold(endpoint_a, first_entered))
        await first_entered.wait()
        second = asyncio.create_task(hold(endpoint_b, second_entered))
        try:
            for _ in range(10):
                if second_entered.is_set():
                    break
                await asyncio.sleep(0)
            assert second_entered.is_set()
        finally:
            release.set()
            await first
            await second

    @pytest.mark.asyncio
    async def test_stt_endpoint_caches_evict_the_least_recently_used_idle_entry(self, monkeypatch):
        """A busy worker must retain active presets without accumulating every past endpoint."""
        from services.inference.parsers import openai_audio as module

        created: list[MagicMock] = []

        def make_openai_client(**_kwargs):
            client = MagicMock()
            client.close = AsyncMock()
            created.append(client)
            return client

        client = _client(MagicMock())
        monkeypatch.setattr(module, "AsyncOpenAI", make_openai_client)
        endpoints = [
            ModelEndpointConfig(
                endpoint=f"http://moss-{index}:8000/v1",
                model_name=f"moss-{index}",
                batch_size=1,
                timeout=900,
                extra={"api_key": f"key-{index}"},
            )
            for index in range(9)
        ]

        async def use(endpoint: ModelEndpointConfig) -> None:
            async with client._transcription_slot(endpoint):
                async with client._transcription_client(endpoint):
                    pass

        for endpoint in endpoints[:8]:
            await use(endpoint)
        await use(endpoints[0])
        await use(endpoints[8])

        assert len(client._endpoint_limiters) == 8
        assert len(client._endpoint_clients) == 8
        assert ("http://moss-0:8000/v1", "moss-0", None) in client._endpoint_limiters
        assert ("http://moss-1:8000/v1", "moss-1", None) not in client._endpoint_limiters
        assert ("http://moss-0:8000/v1", "key-0", 900.0, None) in client._endpoint_clients
        assert ("http://moss-1:8000/v1", "key-1", 900.0, None) not in client._endpoint_clients
        created[1].close.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "updated_fields",
        [
            {"extra": {"api_key": "rotated-key"}},
            {"timeout": 901},
        ],
    )
    async def test_resolved_stt_endpoint_replaces_changed_connection_settings(self, monkeypatch, updated_fields):
        from services.inference.parsers import openai_audio as module

        created: list[MagicMock] = []

        def make_openai_client(**_kwargs):
            client = MagicMock()
            client.audio = MagicMock()
            client.audio.transcriptions = MagicMock()
            client.audio.transcriptions.create = AsyncMock(return_value=MagicMock(text="transcribed"))
            client.close = AsyncMock()
            created.append(client)
            return client

        endpoint = ModelEndpointConfig(
            name="moss",
            endpoint="http://moss:8000/v1",
            model_name="moss-transcribe-diarize",
            timeout=900,
            extra={"api_key": "endpoint-key"},
        )
        selected = [endpoint]
        client = _client(
            MagicMock(),
            transcription_endpoint_resolver=lambda: selected[0],
        )
        monkeypatch.setattr(module, "AsyncOpenAI", make_openai_client)

        await client.parse(_audio_doc())
        selected[0] = endpoint.model_copy(update=updated_fields)
        await client.parse(_audio_doc())

        assert len(created) == 2
        created[0].close.assert_awaited_once()
        created[1].close.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_resolved_stt_endpoint_keeps_an_idle_client_for_another_preset(self, monkeypatch):
        from services.inference.parsers import openai_audio as module

        created: dict[str, MagicMock] = {}
        first_started = asyncio.Event()
        release_first = asyncio.Event()

        def make_openai_client(**kwargs):
            base_url = str(kwargs["base_url"])
            client = MagicMock()
            client.audio = MagicMock()
            client.audio.transcriptions = MagicMock()
            client.close = AsyncMock()
            if base_url == "http://moss-a:8000/v1":

                async def transcribe_a(**_request):
                    first_started.set()
                    await release_first.wait()
                    return MagicMock(text="from a")

                client.audio.transcriptions.create = AsyncMock(side_effect=transcribe_a)
            else:
                client.audio.transcriptions.create = AsyncMock(return_value=MagicMock(text="from b"))
            created[base_url] = client
            return client

        endpoint = ModelEndpointConfig(
            name="moss-a",
            endpoint="http://moss-a:8000/v1",
            model_name="moss",
            timeout=900,
            extra={"api_key": "key-a"},
        )
        selected = [endpoint]
        client = _client(
            MagicMock(),
            transcription_endpoint_resolver=lambda: selected[0],
        )
        monkeypatch.setattr(module, "AsyncOpenAI", make_openai_client)

        first = asyncio.create_task(client.parse(_audio_doc()))
        await asyncio.wait_for(first_started.wait(), timeout=0.5)

        selected[0] = endpoint.model_copy(
            update={
                "name": "moss-b",
                "endpoint": "http://moss-b:8000/v1",
                "extra": {"api_key": "key-b"},
            }
        )
        second = await client.parse(_audio_doc())

        assert second.text_blocks[0].text == "from b"
        created["http://moss-a:8000/v1"].close.assert_not_awaited()
        created["http://moss-b:8000/v1"].close.assert_not_awaited()

        release_first.set()
        first_result = await first

        assert first_result.text_blocks[0].text == "from a"
        created["http://moss-a:8000/v1"].close.assert_not_awaited()
        created["http://moss-b:8000/v1"].close.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_transcribe_exception_propagates(self, mock_openai_client):
        mock_openai_client.audio.transcriptions.create.side_effect = RuntimeError("api down")
        with pytest.raises(RuntimeError, match="api down"):
            await _client(mock_openai_client).parse(_audio_doc())


def test_supported_types(mock_openai_client):
    types_ = _client(mock_openai_client).supported_types()
    assert DocumentType.AUDIO.value in types_
    assert DocumentType.VIDEO.value in types_
