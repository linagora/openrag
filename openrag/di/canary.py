"""Wire the synthetic canary to the services API requests use."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from core.observability.monitoring import CANARY_METRICS
from core.utils.logging import get_logger
from services.orchestrators.canary_service import CANARY_LEASE_KEY, CanaryScheduler, CanaryService
from services.persistence.advisory_lease import AdvisoryLease
from services.storage.postgres_store import PostgresStore

if TYPE_CHECKING:
    from core.config.root import Settings
    from di.container import ServiceContainer

logger = get_logger()


def start_canary(settings: Settings, container: ServiceContainer | None) -> CanaryScheduler | None:
    """Start the canary loop when it is enabled; the caller stops what this returns.

    ``openrag_canary_enabled`` is published even when the container failed to
    come up: an enabled canary that cannot run is exactly what its alert is for.
    """
    config = settings.canary
    CANARY_METRICS.configure(enabled=config.enabled, interval_seconds=config.interval_seconds)
    if not config.enabled:
        return None
    if container is None:
        logger.warning("Synthetic canary enabled but the service container is unavailable; it will not run")
        return None

    service = CanaryService(
        indexing_service=container.indexing_service,
        retrieval_service=container.retrieval_service,
        partition_service=container.partition_service,
        user_repo=container.user_repo,
        document_repo=container.document_repo,
        vector_store=container.vector_store,
        job_repo=container.job_repo,
        settings=settings,
    )
    lease = AdvisoryLease(cast(PostgresStore, container.catalog_store).connect, CANARY_LEASE_KEY)
    scheduler = CanaryScheduler(service=service, lease=lease, config=config)
    scheduler.start()
    logger.info(
        "Synthetic canary scheduled",
        interval_seconds=config.interval_seconds,
        initial_delay_seconds=config.initial_delay_seconds,
    )
    return scheduler
