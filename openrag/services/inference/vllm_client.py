"""vLLM / OpenAI-compatible inference clients.

Three classes grouped by server — all talk to the same OpenAI-compatible API:

* ``VLLMClient``   → ``LLM``      (chat completions)
* ``VLLMEmbedder`` → ``Embedder``  (embeddings)
* ``VLLMVision``   → ``VLM``       (image captioning via chat completions)

Each class has its own circuit breaker instance so an embedder outage
doesn't trip the LLM breaker.
"""

from __future__ import annotations

import asyncio
import base64
import re
from collections.abc import AsyncIterator, Mapping
from urllib.parse import unquote, urlsplit

import httpx
from core.config.endpoints import (
    LLM_OVERRIDE_ENDPOINT_ENV,
    client_llm_override,
    custom_endpoint_override_enabled,
)
from core.embeddings import Embedder, embedder_registry
from core.llm import LLM, llm_registry
from core.utils.exceptions import (
    EmbeddingAPIError,
    EmbeddingConnectionError,
    EmbeddingResponseError,
    EmbeddingTimeoutError,
    InferenceConnectionError,
    InferenceError,
    InferenceTimeoutError,
)
from core.utils.logging import get_logger
from core.vlm import VLM, vlm_registry
from tqdm.asyncio import tqdm

from ._call_log import log_llm_call
from ._circuit_breaker import with_circuit_breaker
from ._retry import with_retry

logger = get_logger()


def _parse_response(resp: httpx.Response) -> dict:
    try:
        return resp.json()
    except ValueError as e:
        raise InferenceError(f"Invalid JSON from inference server ({resp.url}): {e}", status_code=502) from e


# A literal backslash followed by "u" and anything other than 4 hex digits.
# json.dumps always escapes a literal backslash to `\\`, so this pattern can
# only appear in a text's *decoded* content, never in a properly-serialized
# JSON body — but some downstream JSON parsers (observed: a Go-based embedder
# gateway) still choke on it with "invalid character ... in \u hexadecimal
# character escape". Flagging it here pinpoints which input text to inspect.
_SUSPECT_UNICODE_ESCAPE = re.compile(r"\\u(?![0-9a-fA-F]{4})")


def _find_suspect_escapes(texts: list[str], *, limit: int = 5, offset: int = 0) -> list[dict]:
    r"""Locate inputs carrying a suspect ``\u`` escape — at most *limit* of them.

    Findings land in ``EmbeddingAPIError.extra``, which is not log-only:
    error_handlers.py serializes it into the HTTP error body, so these snippets
    are client-visible. Stopping at *limit* keeps that payload bounded — naming
    a few offending chunks is the point, not mirroring every bad input of a
    512-chunk batch.

    *offset* is the position of this batch within the caller's full input, so
    ``index`` identifies the chunk in the document rather than in the batch.
    Without it a bad chunk at position 97 reports as 1 under ``batch_size=32``,
    pointing whoever reads the log at an innocent chunk — and since the log
    carries only these positions (see ``_log_safe_error_detail``), a wrong one
    is the whole diagnostic being wrong.
    """
    findings = []
    for i, text in enumerate(texts):
        match = _SUSPECT_UNICODE_ESCAPE.search(text)
        if match:
            start = max(0, match.start() - 15)
            end = min(len(text), match.end() + 15)
            findings.append({"index": offset + i, "snippet": text[start:end]})
            if len(findings) >= limit:
                break
    return findings


def _log_safe_error_detail(exc: BaseException) -> dict:
    """Summarize a failed embedding call without copying document text to logs.

    ``EmbeddingAPIError.extra`` carries the provider's raw response and, for a
    400, snippets of the indexed document. That detail belongs in the API error,
    whose audience is the uploader who already owns the document — but not in
    container / centralized logs, which operators read across tenants. Keep only
    what makes the failure diagnosable there: the model, the status, and which
    chunk indices look malformed.
    """
    extra = getattr(exc, "extra", None)
    if not isinstance(extra, Mapping):
        return {}
    detail: dict = {
        "model_name": extra.get("model_name"),
        "base_url": extra.get("base_url"),
        "status_code": getattr(exc, "status_code", None),
    }
    suspects = extra.get("suspect_texts") or []
    if suspects:
        # Positions, not snippets — enough to go and inspect the offending
        # chunks without mirroring their text into the log pipeline.
        detail["suspect_count"] = len(suspects)
        detail["suspect_indices"] = [s.get("index") for s in suspects]
    return detail


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------


def _strip_falsy_logprobs(payload: dict) -> dict:
    """Drop an unset/off ``logprobs`` (and its dependent ``top_logprobs``) from *payload*.

    ``logprobs: false`` is already the OpenAI default, so sending it adds
    nothing — but strict providers whose schema lacks the field reject it by
    name whatever the value, e.g. Gemini. Without
    this, the config default (``LLMParamsConfig.logprobs = False``) lands in
    ``self._defaults`` and is sent on every request. A truthy value is a
    deliberate opt-in and is forwarded as-is.

    Checked with ``is`` against ``None``/``False`` rather than plain
    truthiness (or ``in (None, False)``, which suffers the same problem since
    ``0 == False``): the legacy ``/completions`` endpoint's ``logprobs`` is an
    *integer* (how many alternates to return), where ``0`` is a deliberate,
    meaningful request — "give me the sampled token's own logprob, no
    alternates" — not an off state, and must not be conflated with it.
    """
    logprobs = payload.get("logprobs")
    if logprobs is None or logprobs is False:
        payload.pop("logprobs", None)
        payload.pop("top_logprobs", None)
    return payload


@llm_registry.register("vllm")
class VLLMClient(LLM):
    """OpenAI-compatible LLM client backed by vLLM.

    *endpoint* should include the version prefix, e.g. ``http://vllm:8000/v1``.
    A single long-lived ``httpx.AsyncClient`` is reused across requests for
    connection pooling.
    """

    def __init__(
        self,
        endpoint: str,
        model_name: str,
        *,
        api_key: str = "",
        timeout: float = 240.0,
        enable_thinking: bool | None = None,
        max_retries: int = 2,
        **kwargs,
    ) -> None:
        # max_retries is accepted (not forwarded into self._defaults) purely to
        # stop it from leaking into the request body: LLMParamsConfig carries it
        # as a config field, but retry attempts are fixed by the @with_retry
        # decorator on each method, not by a per-instance value. Without this
        # param it would fall into **kwargs like an unknown sampling field and
        # get sent to the backend on every call — the same class of bug fixed
        # for batch_size in di/factories.py (#712).
        del max_retries
        self._endpoint = endpoint.rstrip("/")
        self._model = model_name
        self._api_key = api_key
        self._enable_thinking = enable_thinking
        self._defaults: dict = kwargs
        self._allow_custom_endpoint = custom_endpoint_override_enabled()
        # Authorization per request, not on the client: httpx merges client-level
        # headers into every request with no way to drop one, so the server's key
        # would ride along to an overridden endpoint.
        self._auth_headers: dict[str, str] = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.AsyncClient(timeout=timeout, headers={"Content-Type": "application/json"})
        # Same construction breadcrumb as VLLMEmbedder: the component factories
        # cache instances per endpoint name, so this fires once per configured
        # endpoint and shows which base URL/model a preset name resolved to.
        logger.bind(
            model=self._model,
            endpoint=self._endpoint,
            timeout=timeout,
            enable_thinking=self._enable_thinking,
        ).debug(f"{type(self).__name__} ready")

    def _resolve_overrides(self, kwargs: dict) -> tuple[str, str, dict[str, str], bool]:
        """Read ``metadata.llm_override`` from *kwargs* without mutating caller data.

        Pure read: ``kwargs`` is untouched so retries see the original override on
        every attempt. The caller strips ``metadata`` from the outbound payload via
        ``_payload_kwargs`` — every ``metadata`` key is OpenRAG-internal and never
        belongs on the wire.

        Returns ``(base_url, model, headers, overridden)``. ``overridden`` says the
        endpoint is the client's; callers need it to suppress the server's sampling
        defaults, and reporting it here keeps the override parsed once.
        """
        base_url = self._endpoint
        model = self._model
        headers = self._auth_headers
        overridden = False

        # `model` is always client-overridable; `base_url` / `api_key` only when
        # the operator sets LLM_OVERRIDE_ALLOW_CUSTOM_ENDPOINT.
        llm_override = client_llm_override(kwargs.get("metadata"))
        if llm_override.get("model"):
            model = llm_override["model"]

        if llm_override.get("base_url"):
            if self._allow_custom_endpoint:
                base_url, headers = self._resolve_endpoint_override(llm_override)
                overridden = True
            else:
                # Otherwise the failure is opaque: `model` applies while
                # `base_url` is dropped, so the request hits the *server's*
                # endpoint with a third party's model name and comes back as
                # "invalid model name".
                logger.bind(
                    requested_endpoint=llm_override.get("base_url"),
                    used_endpoint=base_url,
                    model=model,
                ).warning(
                    f"Ignoring llm_override.base_url — {LLM_OVERRIDE_ENDPOINT_ENV} is not enabled. "
                    f"The request goes to the configured endpoint with model={model!r}."
                )
        elif llm_override:
            logger.bind(keys=sorted(llm_override), model=model).warning(
                "llm_override carries no base_url; only the model name is overridden"
            )

        return base_url, model, headers, overridden

    def _resolve_endpoint_override(self, llm_override: Mapping) -> tuple[str, dict[str, str]]:
        """Honor a client-supplied ``llm_override`` endpoint (opt-in, host-unrestricted).

        Restores the pre-refactor contract (``base_url`` + ``api_key`` + ``model``)
        for deployments whose clients rely on it. The *host* is deliberately
        unconstrained; the request *shape* is what keeps this from being a read
        primitive against internal HTTP APIs:

        * **https only** — the plaintext internal services worth reaching (Milvus,
          admin panels) stay unreachable.
        * **No client-controlled path** — always ``{base_url}/chat/completions``,
          or ``{base_url}/completions`` from ``generate``. A ``#`` or ``?`` would
          truncate that suffix once concatenated and a ``..`` would traverse out of
          it, making the override a path-picker. Tested on the raw string, not
          ``urlsplit`` parts: a trailing ``#`` parses as an empty fragment and only
          bites after concatenation.
        * The server's own API key is never forwarded — the override's key, or no
          ``Authorization`` at all.

        Rejections are 4xx so ``with_retry`` (429/502/503/504) does not re-attempt.
        """
        candidate = str(llm_override["base_url"]).strip()

        if "#" in candidate or "?" in candidate:
            raise InferenceError(
                "llm_override.base_url must not carry a query string or fragment",
                code="LLM_OVERRIDE_REJECTED",
                status_code=400,
            )
        try:
            parts = urlsplit(candidate)
        except ValueError as exc:
            # Malformed input, e.g. the unclosed IPv6 literal `https://[::1/v1`.
            # Uncaught it escapes the httpx handlers and error_handlers.py's
            # OpenRAGError mapping, surfacing as an unstructured 500.
            raise InferenceError(
                "llm_override.base_url is not a valid URL",
                code="LLM_OVERRIDE_REJECTED",
                status_code=400,
            ) from exc
        scheme = parts.scheme.lower()
        if scheme != "https":
            raise InferenceError(
                f"llm_override.base_url scheme {scheme!r} is not allowed (https only)",
                code="LLM_OVERRIDE_REJECTED",
                status_code=400,
            )
        # Decoded too: `urlsplit` leaves `%XX` alone and httpx forwards it, so
        # `%2e%2e` arrives as `..` at a target that decodes before routing.
        if ".." in parts.path.split("/") or ".." in unquote(parts.path).split("/"):
            raise InferenceError(
                "llm_override.base_url must not contain a '..' path segment",
                code="LLM_OVERRIDE_REJECTED",
                status_code=400,
            )

        candidate = candidate.rstrip("/")
        api_key = llm_override.get("api_key")
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        logger.bind(endpoint=candidate, configured=self._endpoint).debug(
            "Honoring client-supplied llm_override endpoint"
        )
        return candidate, headers

    def _has_endpoint_override(self, kwargs: dict) -> bool:
        """Is this request routed to a client-supplied endpoint?

        Reads raw ``kwargs`` because the breaker predicate runs before the method
        body. Inside the body, use the flag from ``_resolve_overrides``.
        """
        if not self._allow_custom_endpoint:
            return False
        return bool(client_llm_override(kwargs.get("metadata")).get("base_url"))

    def _chat_payload_kwargs(self, kwargs: dict, *, use_defaults: bool = True) -> dict:
        """Merge server sampling defaults under the request's own params.

        ``use_defaults=False`` for a client-supplied endpoint: the defaults describe
        the *server's* model and another provider may reject them outright (Gemini
        400s on an unsolicited ``logprobs``). Dropping them is what made the restored
        override actually work.
        """
        payload_kwargs = {**self._defaults, **kwargs} if use_defaults else dict(kwargs)
        fallback_thinking = self._enable_thinking if use_defaults else None
        enable_thinking = payload_kwargs.pop("enable_thinking", fallback_thinking)
        if enable_thinking is not None and enable_thinking is True:
            chat_template_kwargs = dict(payload_kwargs.get("chat_template_kwargs") or {})
            chat_template_kwargs.setdefault("enable_thinking", enable_thinking)
            payload_kwargs["chat_template_kwargs"] = chat_template_kwargs
        payload_kwargs = _strip_falsy_logprobs(payload_kwargs)
        return payload_kwargs

    @with_circuit_breaker("llm")
    @with_retry(max_attempts=3)
    async def generate(self, prompt: str, **kwargs) -> dict:
        base_url, model, headers, overridden = self._resolve_overrides(kwargs)
        kwargs.pop("metadata", None)
        payload = {**({} if overridden else self._defaults), **kwargs, "model": model, "prompt": prompt}
        payload = _strip_falsy_logprobs(payload)
        log_llm_call(caller="VLLMClient.generate", model=model, endpoint=base_url, prompt=prompt)
        try:
            resp = await self._client.post(f"{base_url}/completions", json=payload, headers=headers)
            resp.raise_for_status()
        except httpx.ConnectError as exc:
            raise InferenceConnectionError(f"Cannot reach LLM at {base_url}") from exc
        except httpx.TimeoutException as exc:
            raise InferenceTimeoutError(f"LLM request timed out at {base_url}") from exc
        except httpx.HTTPStatusError as exc:
            raise InferenceError(
                f"LLM error ({exc.response.status_code}): {exc.response.text[:500]}",
                status_code=exc.response.status_code,
            ) from exc
        return _parse_response(resp)

    @with_circuit_breaker("llm")
    @with_retry(max_attempts=3)
    async def chat(self, messages: list[dict[str, str]], **kwargs) -> dict:
        base_url, model, headers, overridden = self._resolve_overrides(kwargs)
        kwargs.pop("metadata", None)
        payload = {
            **self._chat_payload_kwargs(kwargs, use_defaults=not overridden),
            "model": model,
            "messages": messages,
            "stream": False,
        }
        log_llm_call(caller="VLLMClient.chat", model=model, endpoint=base_url, messages=messages)
        try:
            resp = await self._client.post(f"{base_url}/chat/completions", json=payload, headers=headers)
            resp.raise_for_status()
        except httpx.ConnectError as exc:
            raise InferenceConnectionError(f"Cannot reach LLM at {base_url}") from exc
        except httpx.TimeoutException as exc:
            raise InferenceTimeoutError(f"LLM request timed out at {base_url}") from exc
        except httpx.HTTPStatusError as exc:
            raise InferenceError(
                f"LLM error ({exc.response.status_code}): {exc.response.text[:500]}",
                status_code=exc.response.status_code,
            ) from exc
        return _parse_response(resp)

    async def stream_chat(self, messages: list[dict[str, str]], **kwargs) -> AsyncIterator[str]:
        base_url, model, headers, overridden = self._resolve_overrides(kwargs)
        kwargs.pop("metadata", None)
        payload = {
            **self._chat_payload_kwargs(kwargs, use_defaults=not overridden),
            "model": model,
            "messages": messages,
            "stream": True,
        }
        log_llm_call(caller="VLLMClient.stream_chat", model=model, endpoint=base_url, messages=messages, stream=True)
        try:
            async with self._client.stream(
                "POST", f"{base_url}/chat/completions", json=payload, headers=headers
            ) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                    raise InferenceError(
                        f"LLM streaming error ({resp.status_code}): {resp.text[:500]}",
                        status_code=resp.status_code,
                    )
                async for line in resp.aiter_lines():
                    yield line
        except httpx.ConnectError as exc:
            raise InferenceConnectionError(f"Cannot reach LLM at {base_url}") from exc
        except httpx.TimeoutException as exc:
            raise InferenceTimeoutError(f"LLM streaming request timed out at {base_url}") from exc

    async def aclose(self) -> None:
        await self._client.aclose()


# ---------------------------------------------------------------------------
# Embedder
# ---------------------------------------------------------------------------


@embedder_registry.register("vllm")
class VLLMEmbedder(Embedder):
    """OpenAI-compatible embedding client backed by vLLM.

    Replaces the sync ``openai.OpenAI`` SDK with an async ``httpx`` client.
    """

    def __init__(
        self,
        endpoint: str,
        model_name: str,
        *,
        max_model_len: int | None = None,
        dimension: int | None = None,
        timeout: float = 120.0,
        batch_size: int = 32,
        embed_concurrency: int = 4,
        api_key: str = "",
        **_kwargs,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._model = model_name
        self._max_model_len = max_model_len
        self._dimension: int | None = dimension
        # Big documents produce thousands of chunks; sending them in one request
        # overruns the endpoint's time budget. Split into `batch_size` slices and
        # run at most `embed_concurrency` of them at once to bound remote load.
        self._batch_size = max(1, batch_size)
        self._embed_concurrency = max(1, embed_concurrency)
        headers: dict[str, str] = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.AsyncClient(timeout=timeout, headers=headers)
        logger.bind(
            model=self._model,
            max_model_len=self._max_model_len,
            batch_size=self._batch_size,
            embed_concurrency=self._embed_concurrency,
        ).debug("VLLMEmbedder ready")
        if self._max_model_len is None:
            logger.bind(model=self._model).warning(
                "VLLMEmbedder built without max_model_len — truncate_prompt_tokens disabled; "
                "pooling models can hang/400 on context-boundary inputs (vllm#29496)."
            )

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed *texts*, splitting large inputs into bounded-concurrent batches.

        A single request for thousands of chunks (e.g. a big PDF) overruns the
        embedder's time budget and trips the client timeout. We slice the input
        into ``batch_size`` chunks and run at most ``embed_concurrency`` requests
        at once, then concatenate the vectors back in input order.
        """
        if not texts:
            return []
        if len(texts) <= self._batch_size:
            return await self._embed_batch(texts)

        # Keep each batch's start index: it is what turns a batch-local suspect
        # position back into a position in `texts`.
        offsets = list(range(0, len(texts), self._batch_size))
        batches = [texts[i : i + self._batch_size] for i in offsets]
        semaphore = asyncio.Semaphore(self._embed_concurrency)

        async def _run(batch: list[str], offset: int) -> list[list[float]]:
            async with semaphore:
                return await self._embed_batch(batch, offset=offset)

        # Explicit tasks so that when one batch fails we can cancel the rest.
        # tqdm.gather (like asyncio.gather) propagates the first exception but
        # leaves the other in-flight batches running, where they would keep
        # consuming embedder capacity after embed() has already failed.
        tasks = [asyncio.ensure_future(_run(batch, offset)) for batch, offset in zip(batches, offsets, strict=True)]
        try:
            results = await tqdm.gather(
                *tasks,
                desc=f"Embedding {len(texts)} chunks ({len(batches)} batches of {self._batch_size})",
            )
        except BaseException as exc:
            done = sum(1 for task in tasks if task.done() and not task.cancelled() and task.exception() is None)
            # `.extra` carries the provider's raw response and, on a 400, snippets
            # of the indexed document. Summarize instead of binding it wholesale:
            # a failed upload of sensitive text must not copy that content into
            # container / centralized logs. The full detail still reaches the
            # uploader on the API error.
            logger.bind(
                batches_done=done,
                n_batches=len(batches),
                error=repr(exc),
                error_detail=_log_safe_error_detail(exc),
            ).warning("Embedding failed after {d}/{b} batches", d=done, b=len(batches))
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        vectors: list[list[float]] = []
        for batch_vectors in results:
            vectors.extend(batch_vectors)
        return vectors

    @with_circuit_breaker("embedder")
    @with_retry(max_attempts=3)
    async def _embed_batch(self, texts: list[str], *, offset: int = 0) -> list[list[float]]:
        body: dict = {"model": self._model, "input": texts}
        if self._max_model_len is not None:
            # Truncate one token *below* max_model_len. vLLM pooling models
            # (e.g. Qwen3-Embedding) hang indefinitely on a request whose input
            # is exactly max_model_len tokens long (vllm-project/vllm#29496).
            # Any chunk >= max_model_len would otherwise be truncated straight
            # onto that boundary, wedging the batch forever while other batches
            # and files keep embedding.
            body["truncate_prompt_tokens"] = max(1, self._max_model_len - 1)
        try:
            resp = await self._client.post(f"{self._endpoint}/embeddings", json=body)
            resp.raise_for_status()
        # Transport failures must carry a retryable status so @with_retry above
        # actually fires (#704) — the translation happens inside the retried
        # body, so the decorator never sees the underlying httpx exception and
        # judges retryability purely on the status code we choose here.
        #
        # TimeoutException is caught first: it is a subclass of TransportError,
        # so the broader clause below would otherwise swallow it as a 503. The
        # TransportError net (not just ConnectError) covers a connection reset
        # mid-request, which httpx raises as ReadError, plus WriteError/protocol
        # errors — all transient and safe to retry since the embed POST is
        # idempotent. HTTPStatusError is not a TransportError, so it is unaffected.
        except httpx.TimeoutException as exc:
            raise EmbeddingTimeoutError(
                f"Embedder request timed out at {self._endpoint}",
                model_name=self._model,
                base_url=self._endpoint,
                error=str(exc),
            ) from exc
        except httpx.TransportError as exc:
            raise EmbeddingConnectionError(
                f"Cannot reach embedder at {self._endpoint}",
                model_name=self._model,
                base_url=self._endpoint,
                error=str(exc),
            ) from exc
        except httpx.HTTPStatusError as exc:
            # Preserve the upstream status, as the LLM path already does, so 429
            # and 5xx are retried while 4xx (bad request, auth) fail fast.
            extra: dict = {
                "model_name": self._model,
                "base_url": self._endpoint,
                "error": exc.response.text[:500],
            }
            # A 400 usually means the embedder rejected the payload itself;
            # pinpoint the offending chunk(s) so operators know which input to
            # inspect (observed: literal `\u…` escapes choking a Go gateway).
            if exc.response.status_code == 400:
                suspect_texts = _find_suspect_escapes(texts, offset=offset)
                if suspect_texts:
                    extra["suspect_texts"] = suspect_texts
            raise EmbeddingAPIError(
                f"Embedder API error ({exc.response.status_code})",
                status_code=exc.response.status_code,
                **extra,
            ) from exc

        try:
            data = resp.json()["data"]
            embeddings = [item["embedding"] for item in sorted(data, key=lambda x: x["index"])]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise EmbeddingResponseError(
                "Unexpected embedding response format",
                model_name=self._model,
                base_url=self._endpoint,
                error=str(exc),
            ) from exc

        if self._dimension is None and embeddings:
            self._dimension = len(embeddings[0])
        return embeddings

    async def embed_single(self, text: str) -> list[float]:
        result = await self.embed([text])
        return result[0]

    @property
    def dimension(self) -> int:
        if self._dimension is None:
            raise RuntimeError("Embedding dimension unknown — call embed() first")
        return self._dimension

    async def aclose(self) -> None:
        await self._client.aclose()


# ---------------------------------------------------------------------------
# VLM (Vision-Language Model)
# ---------------------------------------------------------------------------


@vlm_registry.register("vllm")
class VLLMVision(VLLMClient, VLM):
    """OpenAI-compatible vision client backed by vLLM.

    Inherits connection pooling, retry, and circuit breaker from VLLMClient.
    Adds image captioning via the same OpenAI-compatible chat/completions endpoint.
    """

    def __init__(
        self,
        endpoint: str,
        model_name: str,
        *,
        timeout: float = 60.0,
        api_key: str = "",
        max_tokens: int = 1024,
        **kwargs,
    ) -> None:
        super().__init__(endpoint=endpoint, model_name=model_name, api_key=api_key, timeout=timeout, **kwargs)
        self._max_tokens = max_tokens

    @with_circuit_breaker("vlm")
    @with_retry(max_attempts=2)
    async def caption_image(self, image_bytes: bytes, prompt: str | None = None) -> str:
        image_b64 = base64.b64encode(image_bytes).decode()
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{image_b64}"},
                    },
                    {
                        "type": "text",
                        "text": prompt or "Describe this image in detail.",
                    },
                ],
            }
        ]
        payload: dict = {
            **self._chat_payload_kwargs({}),
            "model": self._model,
            "messages": messages,
            "max_tokens": self._max_tokens,
        }
        log_llm_call(caller="VLLMVision.caption_image", model=self._model, endpoint=self._endpoint, messages=messages)
        try:
            resp = await self._client.post(
                f"{self._endpoint}/chat/completions",
                json=payload,
                # Explicit since Authorization moved off the shared httpx client;
                # captioning always uses the configured endpoint.
                headers=self._auth_headers,
            )
            resp.raise_for_status()
        except httpx.ConnectError as exc:
            raise InferenceConnectionError(f"Cannot reach VLM at {self._endpoint}") from exc
        except httpx.TimeoutException as exc:
            raise InferenceTimeoutError(f"VLM request timed out at {self._endpoint}") from exc
        except httpx.HTTPStatusError as exc:
            raise InferenceError(
                f"VLM error ({exc.response.status_code}): {exc.response.text[:500]}",
                status_code=exc.response.status_code,
            ) from exc
        return _parse_response(resp)["choices"][0]["message"]["content"]

    async def caption_images_batch(self, images: list[bytes], prompt: str | None = None) -> list[str]:
        return list(await asyncio.gather(*(self.caption_image(img, prompt) for img in images)))
