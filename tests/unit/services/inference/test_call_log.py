"""The ``llm.call`` line must show the prompt that goes on the wire — and only
that. It is the operator-facing half of the prompt-wiring check, so it has to
survive multimodal payloads and stay bounded on a context-stuffed chat.
"""

import base64
from contextlib import contextmanager

from core.utils.logging import get_logger
from loguru import logger
from services.inference._call_log import PREVIEW_CHARS, _describe, _render_content, log_llm_call


@contextmanager
def _only_sink(records: list, *, level: str):
    """Swap loguru's handlers for a single sink at *level*, then restore.

    Laziness is a property of the enabled handlers as a whole — loguru evaluates
    a lazy argument if *any* handler accepts the level — so proving the previews
    are skipped means owning the handler set for the duration.
    """
    logger.remove()
    logger.add(records.append, level=level, format="{message}")
    try:
        yield
    finally:
        # ``get_logger`` rebuilds the standard handler set from config (it starts
        # with its own ``logger.remove()``), so the swap leaves nothing behind.
        get_logger()


def test_string_content_is_previewed_with_its_true_length():
    assert _describe({"role": "system", "content": "Answer like a pirate."}) == ("system[21]: Answer like a pirate.")


def test_long_content_is_truncated_to_the_preview_budget():
    rendered = _describe({"role": "user", "content": "x" * 5000})
    # The bracketed size still reports the real payload, so a truncated preview
    # never hides how much context was actually sent.
    assert rendered.startswith("user[5000]: ")
    assert rendered.endswith("…")
    assert len(rendered) < PREVIEW_CHARS + 60


def test_newlines_are_flattened_so_one_call_stays_one_line():
    assert "\n" not in _describe({"role": "system", "content": "line one\nline two\n\nline three"})


def test_image_parts_are_reduced_to_a_marker_and_never_logged():
    image_b64 = base64.b64encode(b"\x89PNG" + b"secret-bytes" * 50).decode()
    content = [
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
        {"type": "text", "text": "Describe this image in detail."},
    ]
    rendered = _render_content(content)
    assert rendered == "<image_url> + Describe this image in detail."
    assert image_b64 not in rendered


def test_unknown_content_shapes_do_not_raise():
    assert _describe({"role": "user", "content": {"weird": object()}})
    assert _describe("not-a-dict")


class _Exploding(str):
    """A payload that fails loudly if its preview is ever built."""

    def __len__(self):
        raise AssertionError("preview built while DEBUG was disabled")


def test_debug_sink_sees_the_prompt_that_goes_on_the_wire():
    records: list[str] = []
    sink_id = logger.add(records.append, level="DEBUG", format="{message}")
    try:
        log_llm_call(
            caller="VLLMClient.chat",
            model="my-model",
            endpoint="http://e",
            messages=[
                {"role": "system", "content": "Aye! Answer like a pirate."},
                {"role": "user", "content": "What are the office hours?"},
            ],
        )
    finally:
        logger.remove(sink_id)

    line = "".join(records)
    assert "llm.call VLLMClient.chat model=my-model stream=False" in line
    assert "system[26]: Aye! Answer like a pirate." in line
    assert "user[26]: What are the office hours?" in line


def test_payload_is_not_duplicated_into_the_record_extras():
    """Regression: a ``detail=`` kwarg lands in ``record["extra"]``, and the
    terminal formatter appends every extra — printing the whole payload twice
    on every line. The preview must reach the message only.
    """
    seen: list = []
    sink_id = logger.add(lambda m: seen.append(m.record), level="DEBUG", format="{message}")
    try:
        log_llm_call(
            caller="VLLMClient.chat",
            model="m",
            endpoint="http://e",
            messages=[{"role": "system", "content": "UNIQUEMARKER pirate instructions"}],
        )
    finally:
        logger.remove(sink_id)

    extras = seen[0]["extra"]
    assert set(extras) == {"caller", "model", "endpoint", "stream"}
    assert not any("UNIQUEMARKER" in str(v) for v in extras.values())
    assert "UNIQUEMARKER" in seen[0]["message"]


def test_previews_are_not_built_when_no_sink_is_at_debug():
    """Lazy evaluation: an INFO-only sink must pay nothing for the previews."""
    records: list[str] = []
    with _only_sink(records, level="INFO"):
        log_llm_call(
            caller="VLLMClient.chat",
            model="m",
            endpoint="http://e",
            messages=[{"role": "system", "content": _Exploding("x")}],
        )
    assert records == []
