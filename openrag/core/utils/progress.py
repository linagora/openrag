"""Progress reporting for concurrent work."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any

from tqdm import tqdm


async def gather_with_progress(*aws: Awaitable[Any], desc: str, return_exceptions: bool = False) -> list[Any]:
    """``asyncio.gather`` with a tqdm bar, ``TQDM_DISABLE=1`` silences it.

    Not ``tqdm.gather``: it abandons its wrapper tasks after the first failure,
    so a second one logs "Task exception was never retrieved" (#1299).
    A child finishing after a sibling's failure closed the bar is harmless:
    ``update`` is a no-op on a closed bar.
    """
    # Bar first: if its first stderr write raises, no child is left running unsupervised.
    with tqdm(total=len(aws), desc=desc) as bar:
        tasks = [asyncio.ensure_future(aw) for aw in aws]
        for task in tasks:
            task.add_done_callback(lambda _: bar.update())
        return await asyncio.gather(*tasks, return_exceptions=return_exceptions)
