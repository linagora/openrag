from __future__ import annotations

import asyncio
import io

import pytest
from core.utils import progress
from core.utils.progress import gather_with_progress
from tqdm import tqdm


@pytest.fixture
def bars(monkeypatch) -> list[tqdm]:
    """Record the bars created, enabled whatever TQDM_DISABLE says (a disabled bar never counts)."""
    created: list[tqdm] = []

    class RecordingBar(tqdm):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, disable=False, file=io.StringIO(), **kwargs)
            created.append(self)

    monkeypatch.setattr(progress, "tqdm", RecordingBar)
    return created


async def test_keeps_input_order_and_reaches_total(bars):
    async def child(value: str, yields: int) -> str:
        for _ in range(yields):
            await asyncio.sleep(0)
        return value

    # The first child finishes last.
    assert await gather_with_progress(child("a", 2), child("b", 0), child("c", 1), desc="d") == ["a", "b", "c"]
    assert bars[0].n == bars[0].total == 3


async def test_child_finishing_after_a_sibling_closed_the_bar_is_harmless(bars):
    errors: list[dict] = []
    asyncio.get_running_loop().set_exception_handler(lambda _loop, ctx: errors.append(ctx))
    release = asyncio.Event()

    async def fail() -> None:
        raise ValueError("boom")

    slow = asyncio.ensure_future(release.wait())

    with pytest.raises(ValueError, match="boom"):
        await gather_with_progress(slow, fail(), desc="d")
    assert not slow.done()

    release.set()
    await slow

    assert errors == []
    assert bars[0].n == 1
