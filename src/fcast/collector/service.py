"""Long-lived collector: owns the database engine and sources, runs on a schedule."""

import asyncio
import contextlib
import logging
import signal
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from fcast.alerts.engine import AlertEngine, build_notifier
from fcast.collector.job import CollectResult, collect_once
from fcast.config import Settings
from fcast.db import migrate
from fcast.db import repositories as repo
from fcast.db.base import utcnow
from fcast.db.session import create_db_engine, create_session_factory
from fcast.sources.base import PriceSource
from fcast.sources.registry import build_sources

logger = logging.getLogger(__name__)

JOB_ID = "collect"


def failed_players(result: CollectResult) -> int | None:
    """Number of cards if a run produced no price at all because sources failed."""
    got_data = result.stored or result.unchanged or result.extinct
    if result.players and not got_data and (result.errors or result.paused or result.skipped):
        return result.players
    return None


class Collector:
    def __init__(
        self,
        settings: Settings,
        sources: Sequence[PriceSource] | None = None,
        alerts: AlertEngine | None = None,
    ) -> None:
        self.settings = settings
        migrate.upgrade(settings.db_url)
        self.engine = create_db_engine(settings.db_url)
        self.session_factory = create_session_factory(self.engine)
        self.sources = (
            list(sources) if sources is not None else build_sources(settings, self._lookup_ref)
        )
        self.alerts = (
            alerts if alerts is not None else AlertEngine(build_notifier(settings), settings)
        )
        self.last_result: CollectResult | None = None
        self._runs = 0
        self._lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._lock.locked()

    async def run_once(self) -> CollectResult:
        async with self._lock:  # never run two collections at the same time
            if not self.sources:
                logger.warning("no price sources configured (FCAST_SOURCES / FCAST_MANUAL_CSV)")
            self.last_result = await collect_once(
                self.session_factory,
                self.sources,
                self.settings.platform,
                rotation=self._runs,
                rotate=self.settings.source_strategy == "rotate",
                pause=timedelta(hours=self.settings.source_pause_h),
                outlier_gap_pct=self.settings.outlier_gap_pct,
            )
            self._runs += 1
            await self._send_alerts(self.last_result)
            return self.last_result

    async def _send_alerts(self, result: CollectResult) -> None:
        # Alerts must never break the collector.
        try:
            await self.alerts.process(
                self.session_factory,
                utcnow(),
                paused=result.paused,
                failed_players=failed_players(result),
            )
        except Exception:
            logger.exception("alert processing failed")

    def _lookup_ref(self, ea_id: int, source: str) -> str | None:
        with self.session_factory() as session:
            return repo.get_source_ref(session, ea_id, source)

    async def aclose(self) -> None:
        for source in self.sources:
            await source.aclose()
        await self.alerts.aclose()
        self.engine.dispose()


def create_scheduler(collector: Collector, interval_min: int, jitter_s: int = 0) -> Any:
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
        jitter=jitter_s or None,
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
    scheduler = create_scheduler(
        collector, settings.collect_interval_min, settings.collect_jitter_s
    )
    scheduler.start()
    logger.info("collector started, interval %d min", settings.collect_interval_min)
    try:
        await stop.wait()
    finally:
        scheduler.shutdown(wait=False)
        await collector.aclose()
        logger.info("collector stopped")
