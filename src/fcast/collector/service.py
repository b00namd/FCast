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
from fcast.collector.job import CollectResult, apply_player_info, collect_once
from fcast.config import Settings
from fcast.db import migrate
from fcast.db import repositories as repo
from fcast.db.base import utcnow
from fcast.db.session import create_db_engine, create_session_factory, session_scope
from fcast.sources.base import PriceSource, SourceBlockedError
from fcast.sources.futbin import FutbinSource
from fcast.sources.registry import build_sources

logger = logging.getLogger(__name__)

JOB_ID = "collect"
FUTBIN = "futbin"
DISCOVERY_PER_RUN = 3
DISCOVERY_RETRY = timedelta(days=1)
DISCOVERY_PREFIX = "futbin.lookup."


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

    def _futbin(self) -> FutbinSource | None:
        return next((s for s in self.sources if isinstance(s, FutbinSource)), None)

    async def discover_futbin_links(
        self,
        limit: int = DISCOVERY_PER_RUN,
        only: Sequence[int] | None = None,
        hints: Sequence[str] = (),
    ) -> list[int]:
        """Look up missing FUTBIN links for watched cards; returns the EA ids that got one.

        Each card is tried at most once per DISCOVERY_RETRY unless named in `only`.
        """
        source = self._futbin()
        if source is None:
            return []
        now = utcnow()
        with session_scope(self.session_factory) as session:
            if FUTBIN in repo.paused_sources(session, now):
                return []
            attempts = repo.get_app_settings(session, DISCOVERY_PREFIX)
            todo: list[tuple[int, list[str]]] = []
            for entry in repo.list_watchlist(session, active_only=True):
                player = entry.player
                if only is not None and player.ea_id not in only:
                    continue
                if repo.get_source_ref(session, player.ea_id, FUTBIN) is not None:
                    continue
                names = [*hints, *([player.name] if player.name else [])]
                if not names:
                    continue  # the name arrives with the first collection run
                last = attempts.get(f"{DISCOVERY_PREFIX}{player.ea_id}")
                if only is None and last and now - datetime.fromisoformat(last) < DISCOVERY_RETRY:
                    continue
                todo.append((player.ea_id, names))
        found: list[int] = []
        for ea_id, names in todo[:limit]:
            try:
                result = await source.discover(ea_id, names)
            except SourceBlockedError as exc:
                with session_scope(self.session_factory) as session:
                    repo.pause_source(
                        session,
                        FUTBIN,
                        now + timedelta(hours=self.settings.source_pause_h),
                        str(exc),
                    )
                logger.warning("FUTBIN refused the lookup, pausing it: %s", exc)
                break
            except Exception:
                logger.exception("FUTBIN lookup for %s failed", ea_id)
                result = None
            with session_scope(self.session_factory) as session:
                repo.set_app_setting(session, f"{DISCOVERY_PREFIX}{ea_id}", now.isoformat())
                if result is not None:
                    path, info = result
                    player = repo.upsert_player(session, ea_id)
                    repo.set_source_ref(session, player, FUTBIN, path)
                    apply_player_info(session, info)
                    found.append(ea_id)
        return found

    async def run_once(self) -> CollectResult:
        # New FUTBIN links first, so this run already uses them.
        try:
            await self.discover_futbin_links()
        except Exception:
            logger.exception("FUTBIN link discovery failed")
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
