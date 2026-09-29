import asyncio
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from fcast.collector.job import collect_once, source_order
from fcast.collector.service import JOB_ID, Collector, create_scheduler, run_forever
from fcast.config import Platform, Settings
from fcast.db import repositories as repo
from fcast.db.base import Base
from fcast.db.models import PriceSnapshot
from fcast.db.session import create_db_engine, create_session_factory
from fcast.sources.base import (
    PlayerInfo,
    PlayerNotFoundError,
    PriceQuote,
    PriceSource,
    SourceError,
)

T0 = datetime(2026, 9, 29, 16, 0, tzinfo=UTC)
FIXTURE = Path(__file__).parent / "fixtures" / "manual" / "prices.csv"


class FakeSource(PriceSource):
    def __init__(
        self,
        name: str,
        prices: dict[int, int] | None = None,
        players: dict[int, PlayerInfo] | None = None,
        fail: bool = False,
        remote: bool = False,
    ) -> None:
        self.name = name
        self.remote = remote
        self.prices = prices or {}
        self.players = players or {}
        self.fail = fail
        self.calls: list[int] = []

    async def fetch_price(self, ea_id: int, platform: Platform) -> PriceQuote:
        self.calls.append(ea_id)
        if self.fail:
            raise SourceError("site down")
        if ea_id not in self.prices:
            raise PlayerNotFoundError(str(ea_id))
        return PriceQuote(ea_id, platform, self.prices[ea_id], self.name, T0)

    async def fetch_player(self, ea_id: int) -> PlayerInfo:
        if self.fail:
            raise SourceError("site down")
        if ea_id not in self.players:
            raise PlayerNotFoundError(str(ea_id))
        return self.players[ea_id]


@pytest.fixture
def factory() -> sessionmaker[Session]:
    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def watch(factory: sessionmaker[Session], *ea_ids: int, active: bool = True) -> None:
    with factory.begin() as session:
        for ea_id in ea_ids:
            player = repo.upsert_player(session, ea_id)
            repo.set_watch(session, player)
            if not active:
                repo.set_watch_active(session, player, False)


def snapshots(factory: sessionmaker[Session]) -> list[tuple[int, int, str]]:
    with factory() as session:
        rows = session.scalars(select(PriceSnapshot).order_by(PriceSnapshot.id)).all()
        return [(s.player_id, s.price, s.source) for s in rows]


async def test_collects_active_watchlist_players(factory: sessionmaker[Session]) -> None:
    watch(factory, 1, 2)
    watch(factory, 3, active=False)
    source = FakeSource("fake", prices={1: 1000, 2: 2000, 3: 3000})

    result = await collect_once(factory, [source], Platform.CONSOLE)

    assert sorted(source.calls) == [1, 2]
    assert result.players == 2
    assert result.stored == 2
    assert result.missing == []
    assert result.errors == {}
    assert result.finished_at is not None
    assert sorted(price for _, price, _ in snapshots(factory)) == [1000, 2000]


async def test_same_quote_is_not_stored_twice(factory: sessionmaker[Session]) -> None:
    watch(factory, 1)
    source = FakeSource("fake", prices={1: 1000})
    await collect_once(factory, [source], Platform.CONSOLE)
    result = await collect_once(factory, [source], Platform.CONSOLE)
    assert result.stored == 0
    assert result.unchanged == 1
    assert len(snapshots(factory)) == 1


async def test_failing_source_does_not_stop_collector(factory: sessionmaker[Session]) -> None:
    watch(factory, 1, 2)
    broken = FakeSource("broken", fail=True)
    fallback = FakeSource("fallback", prices={1: 1000})

    result = await collect_once(factory, [broken, fallback], Platform.CONSOLE)

    assert result.stored == 1
    assert result.missing == [2]
    assert set(result.errors) == {"broken"}
    assert result.error_count == 4  # price + details lookup for both players
    assert [source for _, _, source in snapshots(factory)] == ["fallback"]


async def test_first_remote_source_with_price_wins(factory: sessionmaker[Session]) -> None:
    watch(factory, 1)
    primary = FakeSource("primary", prices={1: 1000}, remote=True)
    secondary = FakeSource("secondary", prices={1: 1100}, remote=True)
    await collect_once(factory, [primary, secondary], Platform.CONSOLE)
    assert secondary.calls == []
    assert snapshots(factory) == [(1, 1000, "primary")]


async def test_remote_fallback_when_first_has_no_price(factory: sessionmaker[Session]) -> None:
    watch(factory, 1)
    primary = FakeSource("primary", remote=True)
    secondary = FakeSource("secondary", prices={1: 1100}, remote=True)
    result = await collect_once(factory, [primary, secondary], Platform.CONSOLE)
    assert result.errors == {}
    assert snapshots(factory) == [(1, 1100, "secondary")]


async def test_local_sources_are_always_read(factory: sessionmaker[Session]) -> None:
    watch(factory, 1)
    manual = FakeSource("manual", prices={1: 900})
    web = FakeSource("web", prices={1: 1000}, remote=True)
    await collect_once(factory, [web, manual], Platform.CONSOLE)
    assert snapshots(factory) == [(1, 900, "manual"), (1, 1000, "web")]


async def test_remote_sources_rotate_per_player_and_run(factory: sessionmaker[Session]) -> None:
    watch(factory, 1, 2, 3, 4)
    a = FakeSource("a", prices=dict.fromkeys(range(1, 5), 100), remote=True)
    b = FakeSource("b", prices=dict.fromkeys(range(1, 5), 100), remote=True)

    await collect_once(factory, [a, b], Platform.CONSOLE, rotation=0)
    assert len(a.calls) == 2
    assert len(b.calls) == 2
    first_run_a = set(a.calls)

    a.calls.clear()
    b.calls.clear()
    await collect_once(factory, [a, b], Platform.CONSOLE, rotation=1)
    assert set(b.calls) == first_run_a  # each player switches source between runs


def test_source_order() -> None:
    local = FakeSource("local")
    r1, r2, r3 = (FakeSource(n, remote=True) for n in ("r1", "r2", "r3"))
    assert [s.name for s in source_order([r1, local, r2, r3], 0)] == ["local", "r1", "r2", "r3"]
    assert [s.name for s in source_order([r1, local, r2, r3], 1)] == ["local", "r2", "r3", "r1"]
    assert [s.name for s in source_order([r1, r2, r3], 5)] == ["r3", "r1", "r2"]
    assert [s.name for s in source_order([local], 7)] == ["local"]


async def test_missing_player_details_are_filled(factory: sessionmaker[Session]) -> None:
    watch(factory, 1)
    info = PlayerInfo(ea_id=1, name="Alpha", rating=88, club="Club")
    await collect_once(factory, [FakeSource("fake", prices={1: 5}, players={1: info})], Platform.PC)
    with factory() as session:
        player = repo.get_player_by_ea_id(session, 1)
        assert player is not None
        assert (player.name, player.rating, player.club) == ("Alpha", 88, "Club")


async def test_empty_watchlist(factory: sessionmaker[Session]) -> None:
    result = await collect_once(factory, [FakeSource("fake")], Platform.CONSOLE)
    assert result.players == 0
    assert result.stored == 0


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    overrides.setdefault("sources", "")  # never enable web sources in tests
    return Settings(_env_file=None, db_path=tmp_path / "fcast.db", **overrides)  # type: ignore[arg-type]


async def test_collector_with_manual_csv(tmp_path: Path) -> None:
    csv_path = tmp_path / "prices.csv"
    shutil.copy(FIXTURE, csv_path)
    collector = Collector(make_settings(tmp_path, manual_csv=csv_path))
    with collector.session_factory.begin() as session:
        repo.set_watch(session, repo.upsert_player(session, 231747))
    try:
        result = await collector.run_once()
    finally:
        await collector.aclose()
    assert result.stored == 1
    assert collector.last_result is result
    with collector.session_factory() as session:
        player = repo.get_player_by_ea_id(session, 231747)
        assert player is not None
        assert player.name == "Kylian Mbappé"
        latest = repo.latest_snapshot(session, player, Platform.CONSOLE)
        assert latest is not None
        assert latest.price == 1_190_000


async def test_scheduler_job_configuration(tmp_path: Path) -> None:
    collector = Collector(make_settings(tmp_path), sources=[])
    scheduler = create_scheduler(collector, 15)
    job = scheduler.get_job(JOB_ID)
    assert job.trigger.interval.total_seconds() == 15 * 60
    assert job.max_instances == 1
    assert job.coalesce is True
    await collector.aclose()


async def test_run_forever_runs_immediately_and_stops(tmp_path: Path) -> None:
    csv_path = tmp_path / "prices.csv"
    shutil.copy(FIXTURE, csv_path)
    settings = make_settings(tmp_path, manual_csv=csv_path)
    setup = Collector(settings)
    with setup.session_factory.begin() as session:
        repo.set_watch(session, repo.upsert_player(session, 158023))
    await setup.aclose()

    stop = asyncio.Event()
    task = asyncio.create_task(run_forever(settings, stop))
    engine = create_db_engine(settings.db_url)
    try:
        for _ in range(100):  # the first run is scheduled for "now"
            await asyncio.sleep(0.05)
            with engine.connect() as connection:
                if connection.scalar(select(PriceSnapshot.id)) is not None:
                    break
        else:
            pytest.fail("collector did not run")
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=5)
        engine.dispose()
