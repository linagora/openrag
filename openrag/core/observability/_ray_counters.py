"""Start a ``ray.util.metrics.Counter`` series at 0.

A counter series first scraped at 1 loses that first event: ``rate()`` and
``increase()`` count the change between samples, and there is no earlier one.
Every Ray worker process exports its own series (``WorkerId``), so without a
zero start each worker's first failure disappears from every ratio.

``prometheus_client`` accepts ``inc(0)``. Ray's public ``Counter.inc`` refuses
it (``value must be >0``), so this records through ``_record`` — the method
``inc`` itself calls once its check passes. That is a private Ray API:
``tests/integration/test_ray_metrics_export.py`` asserts the zero series reaches
the export endpoint, so a Ray upgrade that changes it fails CI rather than
silently dropping the first event again.

No module-level Ray import: this is shared with modules that load Ray lazily.
"""

from __future__ import annotations

from typing import Any


def start_counter_at_zero(counter: Any, tags: dict[str, str] | None = None) -> None:
    """Create ``counter``'s series for ``tags`` at 0 without counting anything."""
    counter._record(0, tags=tags)
