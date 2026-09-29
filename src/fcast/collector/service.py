"""Long-lived collector: owns the database engine and sources, runs on a schedule."""

import asyncio
import contextlib
import logging
import signal
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from fcast.collector.job import CollectResult, collect_once
from fcast.config import Settings
from fcast.db import migrate
from fcast.db.session import create_db_engine, create_session_factory
from fcast.sources.base import PriceSource
from fcast.sources.registry import build_sources

logger = logging.getLogger(__name__)

JOB_ID = "collect"


class Collector:
    def __init__(self, settings: Settings, sources: Sequence[PriceSource] | None = None) -> None:
        self.settings = settings
        migrate.upgrade(settings.db_url)
        self.engine = create_db_engine(settings.db_url)
        self.session_factory = create_session_factory(self.engine)
        self.sources = list(sources) if sources is not None else build_sources(settings)
        self.last_result: CollectResult | None = None
        self._lock = asyncio.Lock()

    async def run_once(self) -> CollectResult:
        async with self._lock:  # never run two collections at the same time
            if not self.sources:
                logger.warning("no price sources configured (set FCAST_MANUAL_CSV)")
            self.last_result = await collect_once(
                self.session_factory, self.sources, self.settings.platform
            )
            return self.last_result

    async def aclose(self) -> None:
        for source in self.sources:
            await source.aclose()
        self.engine.dispose()


def create_scheduler(collector: Collector, interval_min: int) -> Any:
    scheduler = AsyncIOScheduler(timezone=UTC)
    scheduler.add_job(
        collector.run_once,
        "interval",
        minutes=interval_min,
        id=JOB_ID,
        next_run_time=datetime.now(UTC),
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )
    return scheduler


async def run_forever(settings: Settings, stop: asyncio.Event | None = None) -> None:
    """Run the collector on its interval until `stop` is set or SIGTERM/SIGINT arrives."""
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        # Not supported on Windows or outside the main thread.
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop.set)

    collector = Collector(settings)
    scheduler = create_scheduler(collector, settings.collect_interval_min)
    scheduler.start()
    logger.info("collector started, interval %d min", settings.collect_interval_min)
    try:
        await stop.wait()
    finally:
        scheduler.shutdown(wait=False)
        await collector.aclose()
        logger.info("collector stopped")
