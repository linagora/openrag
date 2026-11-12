"""The context window and output budget of the LLM endpoint answering a request.

``get_max_model_tokens`` resolves the window in three tiers: the endpoint's
admin-configured ``max_llm_context_size``, else the ``max_model_len`` its
``/v1/models`` reported (probed by ``prime_max_model_tokens`` at startup and
after every endpoint write, cached per endpoint name), else the global
``llm_context`` fallback. The endpoint is the partition's ``chat_llm`` when the
request's partitions agree on one (``resolve_partition_chat_llm``, the rule
``QueryService`` answers with), else the ``"default"`` alias.

The probe cache is process state: the API's lifespan primes it, and the admin
model-endpoint routes invalidate and re-prime it.
"""

import asyncio
from typing import TYPE_CHECKING

from core.config import load_config
from core.models.preset import resolve_partition_chat_llm
from core.utils.logging import get_logger
from openai import AsyncOpenAI

if TYPE_CHECKING:
    from core.config.root import Settings

logger = get_logger()


async def get_openai_models(base_url: str, api_key: str, timeout: float = 30):
    """The model list an OpenAI-compatible endpoint serves (``/v1/models``)."""
    async with AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=timeout) as client:
        models_response = await client.models.list()
        return models_response.data


# Auto-probed max model token limits, keyed by LLM endpoint registry name.
# Populated by ``prime_max_model_tokens`` which the application lifespan
# invokes during startup, once the model-endpoint registry is loaded. It is
# only the *middle* tier of ``get_max_model_tokens``: an admin-configured
# context size on the resolved LLM endpoint takes precedence, and
# ``config.llm_context`` is the final fallback.
_max_model_tokens_by_name: dict[str, int] = {}

# Serializes ``prime_max_model_tokens`` refreshes. Model-endpoint writes each
# schedule a refresh (best-effort, off the request path), so several can be
# in flight at once; without this lock a slower, older probe could finish last
# and overwrite a newer refresh's result, leaving the cache stale. Holding the
# lock across the snapshot + probe means whichever refresh runs last observes
# the latest registry state and publishes the newest result.
_max_model_tokens_lock = asyncio.Lock()

# Bumped by every ``invalidate_max_model_tokens`` call. A refresh captures the
# generation before probing and discards its results if the value moved while
# it was in flight — the registry it probed is no longer the current one, and a
# newer refresh is already queued behind the lock. Without this an in-flight
# probe that started before an endpoint write would republish pre-write values
# on top of the invalidation, re-opening the stale window it just closed.
_max_model_tokens_generation = 0

# True from an invalidation until the refresh it scheduled publishes. While it
# holds, an empty cache entry means "not probed yet", not "the endpoint reports
# no max_model_len", so the admin UI can say the detection is still running
# instead of showing the global fallback for an endpoint that does report one.
_max_model_tokens_pending = False


def probed_max_model_tokens(name: str) -> int | None:
    """The ``max_model_len`` LLM endpoint *name* reported on ``/v1/models``, if any."""
    return _max_model_tokens_by_name.get(name)


def max_model_tokens_probe_pending() -> bool:
    """Whether an endpoint write cleared the probed limits and their re-probe hasn't landed yet."""
    return _max_model_tokens_pending


def _runtime_config(settings: "Settings | None" = None) -> "Settings":
    return settings if settings is not None else load_config()


def invalidate_max_model_tokens() -> None:
    """Drop every auto-probed limit, synchronously.

    Model-endpoint writes refresh ``config.models.llm`` *synchronously* (inside
    ``ModelEndpointService.load_all``) but re-probe ``/v1/models`` in a
    background task, so between the two the registry describes the new endpoint
    while this cache still holds the previous one's ``max_model_len``. A chat
    request landing in that window would be answered by the new endpoint and
    preflighted against the old endpoint's limit — precisely the
    preflight/answer divergence the partition-scoped resolution exists to
    prevent, and the window is as wide as the probes are slow (an unreachable
    endpoint costs its full timeout).

    Clearing here collapses that window: until the refresh lands,
    ``get_max_model_tokens`` falls through to the endpoint's admin-configured
    size (still read live from the registry, so unaffected) or the global
    ``llm_context`` fallback — an intentionally conservative "not probed yet",
    the same state the cache is in before startup priming, rather than a
    confidently wrong value belonging to a different endpoint.

    Deliberately not locked: rebinding the dict is atomic, so a concurrent
    reader sees either the old mapping or the empty one, never a half-built
    dict — and taking ``_max_model_tokens_lock`` here would make an admin write
    wait behind an in-flight probe, which is the stall the background task was
    introduced to avoid.
    """
    global _max_model_tokens_by_name, _max_model_tokens_generation, _max_model_tokens_pending
    _max_model_tokens_generation += 1
    _max_model_tokens_by_name = {}
    _max_model_tokens_pending = True


async def prime_max_model_tokens(settings: "Settings | None" = None) -> None:
    """Populate the per-endpoint auto-probed max model token cache.

    Called once from the FastAPI lifespan in ``api/main.py`` (replaces the
    deprecated ``@router.on_event("startup")`` hook), after the model-endpoint
    registry is loaded. Probes every registered LLM endpoint's ``/v1/models``
    for ``max_model_len`` — not just the default — so a partition's
    ``chat_llm`` endpoint gets its own auto-probed budget instead of silently
    falling back to the global ``llm_context`` default. Endpoints that alias
    the same underlying config (the ``"default"`` name always points at
    whichever endpoint is ``is_default``) are probed once and the result
    cached under every alias; endpoints with no ``model_name`` can't be
    matched in the ``/v1/models`` listing and are skipped. One endpoint's
    probe failure doesn't affect the others. Safe to call again — it just
    refreshes the cache.

    Serialized under ``_max_model_tokens_lock`` so overlapping refreshes
    (several endpoint writes in quick succession) can't publish a stale cache:
    each refresh reads the live registry and probes while holding the lock, so
    the last refresh to run always observes the latest state and wins. A
    refresh whose generation was invalidated mid-probe discards its results
    rather than publishing them over the invalidation.
    """
    global _max_model_tokens_by_name, _max_model_tokens_pending
    async with _max_model_tokens_lock:
        generation = _max_model_tokens_generation
        config = _runtime_config(settings)
        probed_by_identity: dict[int, int | None] = {}
        results: dict[str, int] = {}
        # Snapshot the endpoint dict up front: a concurrent model-endpoint reload
        # replaces its contents in place (ModelEndpointService.load_all does
        # dict.clear() + dict.update()), so iterating the live dict across the
        # `await` below could raise "dictionary changed size during iteration".
        try:
            for name, endpoint in list(config.models.llm.items()):
                if not endpoint.model_name:
                    continue
                identity = id(endpoint)
                if identity not in probed_by_identity:
                    probed_by_identity[identity] = await _fetch_max_model_tokens(
                        base_url=endpoint.endpoint,
                        model_id=endpoint.model_name,
                        api_key=endpoint.extra.get("api_key", ""),
                        # Honour the endpoint's own configured timeout rather than
                        # the probe helper's generic default: probes run serially
                        # under the lock, so an unreachable endpoint holds up every
                        # later refresh for its full timeout.
                        timeout=endpoint.timeout,
                    )
                value = probed_by_identity[identity]
                if value is not None:
                    results[name] = value
        except Exception:
            # The cache stays as the invalidation left it, so requests use the
            # global fallback: report that rather than "detecting" forever.
            if generation == _max_model_tokens_generation:
                _max_model_tokens_pending = False
            raise
        if generation != _max_model_tokens_generation:
            # An endpoint write invalidated the cache while these probes were in
            # flight, so `results` describes a registry that is already gone.
            # Drop them; the write scheduled its own refresh behind this lock.
            logger.debug("Discarding auto-probed LLM token results invalidated mid-refresh")
            return
        _max_model_tokens_by_name = results
        _max_model_tokens_pending = False


async def _fetch_max_model_tokens(
    *, base_url: str, model_id: str, api_key: str, timeout: float | None = None
) -> int | None:
    """Fetch one endpoint's max model token limit from its ``/v1/models`` listing.

    Returns ``None`` — not a fallback value — when the endpoint is
    unreachable, doesn't serve ``model_id``, or doesn't report
    ``max_model_len``: this probes a single endpoint among potentially many,
    so the fallback decision belongs to the caller (``get_max_model_tokens``),
    not this helper.

    *timeout* defaults to ``get_openai_models``' own when omitted; callers that
    know the endpoint's configured timeout should pass it, so a slow endpoint
    is bounded by the value the admin set for it.
    """
    try:
        timeout_kwargs = {} if timeout is None else {"timeout": timeout}
        openai_models = await get_openai_models(base_url=base_url, api_key=api_key, **timeout_kwargs)
        model = next((m for m in openai_models if m.id == model_id), None)
        if model is None:
            logger.warning(f"No model found for {model_id} at {base_url}.")
            return None
        # `max_model_len` is a vendor extension vLLM adds to the OpenAI /v1/models
        # entry. The SDK model allows extras, and pydantic dumps those at the top
        # level — `model_extra` is an instance property, never a key in the dump —
        # so a top-level lookup is the only one that can ever match.
        model_data = model.model_dump() if hasattr(model, "model_dump") else model.dict()
        max_len = model_data.get("max_model_len")
        if max_len is None:
            logger.warning(f"max_model_len not found for {model_id} at {base_url}.")
            return None
        logger.info(
            "Fetched max_model_len from vLLM at startup", model=model_id, base_url=base_url, max_model_len=int(max_len)
        )
        return int(max_len)
    except Exception as e:
        logger.warning("Failed to query /v1/models for max_model_len", base_url=base_url, model=model_id, error=str(e))
        return None


def resolve_llm_endpoint_name(config: "Settings", partitions: list[str] | None) -> str:
    """Effective LLM endpoint registry name for *partitions*.

    Delegates to ``resolve_partition_chat_llm`` — the same consensus rule
    ``QueryService._resolve_llm`` uses to pick the LLM that actually answers
    the request — so the token preflight is checked against the budget of the
    model that will really be called, not always the global default. Falls
    back to the ``"default"`` alias (see ``ModelEndpointService``) when no
    single partition-scoped endpoint applies.

    A partition's ``chat_llm`` can also go **stale** — the endpoint it names
    may have been deleted or renamed after assignment (nothing cascades the
    reference). ``QueryService._resolve_llm`` handles that by catching the
    factory ``KeyError`` and answering with the catalog default; the preflight
    must converge on the same endpoint, otherwise it would check the request
    against the global budget while a differently-sized default endpoint
    answers it. So an unresolvable name falls back to ``"default"`` here too.
    """
    name = resolve_partition_chat_llm(partitions, config.partitions)
    if name is None or name not in config.models.llm:
        return "default"
    return name


def effective_max_output_tokens(config: "Settings", partitions: list[str] | None = None) -> int:
    """Default output-token budget: the resolved LLM endpoint's admin-configured
    value when set, else the global ``llm_context`` fallback."""
    configured = config.models.llm_output_tokens(resolve_llm_endpoint_name(config, partitions))
    return configured or int(config.llm_context.max_output_tokens)


def get_max_model_tokens(partitions: list[str] | None = None, settings: "Settings | None" = None) -> int:
    """Effective max context size for the token preflight.

    Precedence: the resolved LLM endpoint's admin-configured
    ``max_llm_context_size`` (editable in the admin UI; resolved from the
    partition's ``chat_llm`` endpoint when the request is scoped to partitions
    that agree on one, else the "default" endpoint) > the value auto-probed
    from that same endpoint's ``/v1/models`` at startup (see
    ``prime_max_model_tokens`` — cached per endpoint name, not just the
    default) > the global ``llm_context`` fallback.
    """
    config = _runtime_config(settings)
    name = resolve_llm_endpoint_name(config, partitions)
    configured = config.models.llm_context_size(name)
    if configured is not None:
        return configured
    probed = _max_model_tokens_by_name.get(name)
    return probed or int(config.llm_context.max_llm_context_size)


__all__ = [
    "effective_max_output_tokens",
    "get_max_model_tokens",
    "get_openai_models",
    "invalidate_max_model_tokens",
    "max_model_tokens_probe_pending",
    "prime_max_model_tokens",
    "probed_max_model_tokens",
    "resolve_llm_endpoint_name",
]
