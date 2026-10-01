import asyncio
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from fcast.collector.job import collect_once, source_order
from fcast.collector.service import JOB_ID, Collector, create_scheduler, run_forever
from fcast.config import Platform, Settings
from fcast.db import repositories as repo
from fcast.db.base import Base
from fcast.db.models import MarketState, PriceSnapshot, SourceStatus
from fcast.db.session import create_db_engine, create_session_factory
from fcast.sources.base import (
    ExtinctError,
    MarketInfo,
    PlayerInfo,
    PlayerNotFoundError,
    PriceQuote,
    PriceSource,
    SourceBlockedError,
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

    await collect_once(factory, [a, b], Platform.CONSOLE, rotation=0, rotate=True)
    assert len(a.calls) == 2
    assert len(b.calls) == 2
    first_run_a = set(a.calls)

    a.calls.clear()
    b.calls.clear()
    await collect_once(factory, [a, b], Platform.CONSOLE, rotation=1, rotate=True)
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


class BlockingSource(FakeSource):
    """Remote source that refuses every request like a site that blocked us."""

    def __init__(self, name: str = "blocked") -> None:
        super().__init__(name, remote=True)

    async def fetch_price(self, ea_id: int, platform: Platform) -> PriceQuote:
        self.calls.append(ea_id)
        raise SourceBlockedError("HTTP 429")


def statuses(factory: sessionmaker[Session]) -> dict[str, SourceStatus]:
    with factory() as session:
        return {s.source: s for s in repo.list_source_statuses(session)}


async def test_blocked_source_is_paused_and_fallback_used(factory: sessionmaker[Session]) -> None:
    watch(factory, 1, 2, 3)
    blocked = BlockingSource()
    fallback = FakeSource("fallback", prices={1: 10, 2: 20, 3: 30}, remote=True)

    result = await collect_once(
        factory, [blocked, fallback], Platform.PC, pause=timedelta(hours=24)
    )

    assert blocked.calls == [1]  # never asked again during the run
    assert result.stored == 3
    assert result.paused == {"blocked": "HTTP 429"}
    assert result.errors == {}
    status = statuses(factory)["blocked"]
    assert status.paused_until is not None
    assert status.paused_until - result.finished_at == timedelta(hours=24)  # type: ignore[operator]
    assert status.pause_reason == "HTTP 429"
    assert statuses(factory)["fallback"].last_success_at is not None


async def test_paused_source_is_skipped_until_pause_ends(factory: sessionmaker[Session]) -> None:
    watch(factory, 1)
    source = FakeSource("web", prices={1: 10}, remote=True)
    with factory.begin() as session:
        repo.pause_source(session, "web", datetime.now(UTC) + timedelta(hours=1), "HTTP 403")

    result = await collect_once(factory, [source], Platform.PC)
    assert source.calls == []
    assert result.skipped == ["web"]
    assert result.missing == [1]

    with factory.begin() as session:
        repo.pause_source(session, "web", datetime.now(UTC) - timedelta(seconds=1), "HTTP 403")
    result = await collect_once(factory, [source], Platform.PC)
    assert source.calls == [1]
    assert result.stored == 1


async def test_resume_source(factory: sessionmaker[Session]) -> None:
    with factory.begin() as session:
        repo.pause_source(session, "web", datetime.now(UTC) + timedelta(hours=1), "x")
        repo.resume_source(session, "web")
        assert repo.paused_sources(session, datetime.now(UTC)) == set()


async def test_errors_are_recorded_in_source_status(factory: sessionmaker[Session]) -> None:
    watch(factory, 1)
    await collect_once(factory, [FakeSource("broken", fail=True, remote=True)], Platform.PC)
    status = statuses(factory)["broken"]
    assert status.last_error is not None
    assert "site down" in status.last_error
    assert status.last_success_at is None
    assert status.paused_until is None  # ordinary errors do not pause a source


async def test_priority_strategy_always_asks_first_source(factory: sessionmaker[Session]) -> None:
    watch(factory, 1, 2, 3)
    first = FakeSource("first", prices=dict.fromkeys(range(1, 4), 5), remote=True)
    second = FakeSource("second", prices=dict.fromkeys(range(1, 4), 6), remote=True)
    for run in range(2):
        await collect_once(factory, [first, second], Platform.PC, rotation=run)
    assert second.calls == []
    assert len(first.calls) == 6


async def test_scheduler_jitter(tmp_path: Path) -> None:
    collector = Collector(make_settings(tmp_path), sources=[])
    scheduler = create_scheduler(collector, 30, jitter_s=180)
    assert scheduler.get_job(JOB_ID).trigger.jitter == 180
    await collector.aclose()


class MarketSource(FakeSource):
    """Remote source that reports supply details like FUTBIN."""

    def __init__(self, listings: dict[int, tuple[int, ...]], range_max: int = 1_000_000) -> None:
        super().__init__("depth", remote=True)
        self.listings = listings
        self.range_max = range_max

    async def fetch_price(self, ea_id: int, platform: Platform) -> PriceQuote:
        self.calls.append(ea_id)
        market = MarketInfo(listings=self.listings[ea_id], range_min=150, range_max=self.range_max)
        if market.extinct:
            raise ExtinctError("extinct", market)
        return PriceQuote(ea_id, platform, market.listings[0], self.name, T0, market=market)


def market_state(factory: sessionmaker[Session], ea_id: int) -> MarketState | None:
    with factory() as session:
        player = repo.get_player_by_ea_id(session, ea_id)
        assert player is not None
        return repo.get_market_state(session, player, Platform.PC)


async def test_market_state_is_recorded(factory: sessionmaker[Session]) -> None:
    watch(factory, 1)
    await collect_once(factory, [MarketSource({1: (10_000, 10_500, 11_000)})], Platform.PC)
    state = market_state(factory, 1)
    assert state is not None
    assert state.listings == (10_000, 10_500, 11_000)
    assert state.range_max == 1_000_000
    assert not state.extinct


async def test_outlier_listing_is_dampened(factory: sessionmaker[Session]) -> None:
    watch(factory, 1)
    await collect_once(
        factory, [MarketSource({1: (5_500, 8_150, 8_200)})], Platform.PC, outlier_gap_pct=15
    )
    assert snapshots(factory) == [(1, 8_150, "depth")]
    state = market_state(factory, 1)
    assert state is not None
    assert state.listings[0] == 5_500  # the raw outlier stays visible


async def test_extinct_stops_fallback_and_is_tracked(factory: sessionmaker[Session]) -> None:
    watch(factory, 1)
    depth = MarketSource({1: ()})
    fallback = FakeSource("fallback", prices={1: 6_800_000}, remote=True)

    result = await collect_once(factory, [depth, fallback], Platform.PC)

    assert result.extinct == [1]
    assert result.missing == []
    assert fallback.calls == []  # no stale price from the fallback
    assert snapshots(factory) == []
    state = market_state(factory, 1)
    assert state is not None
    assert state.extinct
    first_since = state.extinct_since
    assert first_since is not None

    # Still extinct later: the start time is kept. Listed again: it is cleared.
    await collect_once(factory, [depth], Platform.PC)
    state = market_state(factory, 1)
    assert state is not None
    assert state.extinct_since == first_since
    depth.listings[1] = (7_000_000,)
    await collect_once(factory, [depth], Platform.PC)
    state = market_state(factory, 1)
    assert state is not None
    assert state.extinct_since is None


async def test_extra_cards_are_collected_like_watchlist_cards(
    factory: sessionmaker[Session],
) -> None:
    watch(factory, 1)
    source = FakeSource("fake", prices={1: 1000, 7: 7000})
    result = await collect_once(factory, [source], Platform.PC, extra_ea_ids=[7, 1])
    assert result.players == 2  # the watched card is not counted twice
    assert sorted(source.calls) == [1, 7]
    assert sorted(price for _, price, _ in snapshots(factory)) == [1000, 7000]


async def test_slow_cards_are_collected_only_when_due(factory: sessionmaker[Session]) -> None:
    watch(factory, 1, 2)
    with factory.begin() as session:
        slow = repo.get_player_by_ea_id(session, 2)
        assert slow is not None
        repo.set_watch_interval(session, slow, 120)
    source = FakeSource("fake", prices={1: 1000, 2: 2000})

    first = await collect_once(factory, [source], Platform.CONSOLE)
    assert sorted(source.calls) == [1, 2]  # never checked: due
    assert first.deferred == 0

    source.calls.clear()
    second = await collect_once(factory, [source], Platform.CONSOLE)
    assert source.calls == [1]  # the slow card was just checked
    assert (second.players, second.deferred) == (1, 1)

    with factory.begin() as session:  # two hours later (minus the jitter tolerance) it is due
        player = repo.get_player_by_ea_id(session, 2)
        assert player is not None
        entry = repo.get_watch(session, player)
        assert entry is not None and entry.checked_at is not None
        entry.checked_at -= timedelta(minutes=116)
    source.calls.clear()
    await collect_once(factory, [source], Platform.CONSOLE)
    assert sorted(source.calls) == [1, 2]


def test_is_due() -> None:
    from fcast.db.models import WatchlistEntry

    now = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    entry = WatchlistEntry(interval_min=None, checked_at=now)
    assert repo.is_due(entry, now)  # every run
    entry.interval_min = 360
    assert not repo.is_due(entry, now + timedelta(hours=5))
    assert repo.is_due(entry, now + timedelta(hours=5, minutes=56))
    entry.checked_at = None
    assert repo.is_due(entry, now)
