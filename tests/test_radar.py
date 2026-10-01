"""Potential radar: FUTBIN list parsing and early signals on constructed series."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from sqlalchemy import select

from fcast.analysis.stats import Point
from fcast.collector.service import Collector
from fcast.config import Platform, Settings
from fcast.db import repositories as repo
from fcast.db.models import FodderPrice, RadarCard
from fcast.radar import service as radar
from fcast.radar import signals as rs
from fcast.radar.futbin_lists import parse_fodder, parse_short_price, player_refs
from fcast.sources.futbin import FutbinSource
from fcast.sources.http import PoliteHttpClient

FIXTURES = Path(__file__).parent / "fixtures" / "futbin"
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
H = timedelta(hours=1)
CFG = rs.RadarConfig()


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def hourly(prices: list[int]) -> list[Point]:
    """Prices ending at NOW, one per hour."""
    return [(NOW - (len(prices) - 1 - i) * H, p) for i, p in enumerate(prices)]


# --- FUTBIN lists -------------------------------------------------------------------------


def test_player_refs_from_list_pages() -> None:
    popular = player_refs(fixture("popular.html"))
    assert len(popular) == 40
    assert popular[:2] == ["/27/player/23067/joao-felix-sequeira", "/27/player/115/georgia-stanway"]
    latest = player_refs(fixture("latest.html"), limit=3)
    assert latest == [
        "/27/player/23099/milan-iloski",
        "/27/player/23098/phillipp-mwene",
        "/27/player/23097/shea-charles",
    ]


def test_fodder_prices_per_rating_and_platform() -> None:
    pc = parse_fodder(fixture("cheapest.html"), Platform.PC)
    assert pc[85][:3] == [1_800, 1_900, 1_900]
    assert pc[88][:3] == [9_400, 9_500, 9_600]
    assert 96 not in pc  # no cards listed
    console = parse_fodder(fixture("cheapest.html"), Platform.CONSOLE)
    assert console[87][:3] == [5_000, 5_000, 5_400]


def test_short_prices() -> None:
    cases = {"650": 650, "1.8K": 1_800, "10.25K": 10_250, "2.18M": 2_180_000, "1,500": 1_500}
    for text, value in cases.items():
        assert parse_short_price(text) == value
    assert parse_short_price("-") is None


# --- TREND_START --------------------------------------------------------------------------


def test_trend_start_on_a_steady_rise() -> None:
    points = hourly([20_000] * 12 + [20_000, 20_250, 20_500, 20_500, 21_000, 21_250, 21_500])
    signal = rs.trend_start(points, NOW, CFG)
    assert signal is not None
    assert signal.kind is rs.RadarKind.TREND_START
    assert signal.score >= 70
    assert "+7,5 % in 12 h" in signal.reasons[0]


def test_no_trend_start_when_flat_jumped_or_falling_back() -> None:
    assert rs.trend_start(hourly([20_000] * 13), NOW, CFG) is None
    jumped = hourly([20_000] * 12 + [30_000])  # +50 %: too late
    assert rs.trend_start(jumped, NOW, CFG) is None
    zigzag = hourly([20_000, 21_500, 20_000, 21_500, 20_000, 21_500, 20_000, 21_500])
    assert rs.trend_start(zigzag, NOW, CFG) is None
    spike = hourly([20_000, 20_500, 21_000, 21_500, 22_500, 21_200])  # off its high
    assert rs.trend_start(spike, NOW, CFG) is None
    assert rs.trend_start(hourly([20_000, 21_500]), NOW, CFG) is None  # too few points


# --- SUPPLY_SHRINKING ----------------------------------------------------------------------


def counts(values: list[int]) -> list[tuple[datetime, int]]:
    return [(NOW - (len(values) - 1 - i) * 4 * H, v) for i, v in enumerate(values)]


def test_supply_shrinking_towards_extinct() -> None:
    points = hourly([10_000] * 20 + [10_200])
    signal = rs.supply_shrinking(counts([5, 5, 4, 2, 0]), points, NOW, CFG)
    assert signal is not None
    assert signal.reasons[:2] == ("Angebote 5 → 0 in 16 h", "jetzt extinct")
    smaller = rs.supply_shrinking(counts([5, 4, 3]), points, NOW, CFG)
    assert smaller is not None and smaller.score < signal.score


def test_no_supply_signal_when_full_or_price_falling() -> None:
    points = hourly([10_000] * 21)
    assert rs.supply_shrinking(counts([5, 5, 5]), points, NOW, CFG) is None
    assert rs.supply_shrinking(counts([5, 4, 5]), points, NOW, CFG) is None
    falling = hourly([10_000] * 20 + [9_000])
    assert rs.supply_shrinking(counts([5, 4, 2]), falling, NOW, CFG) is None


# --- USAGE_SURGE ----------------------------------------------------------------------------


def usage(per_day_before: int, per_day_now: int) -> list[tuple[datetime, int]]:
    """Games counter every 6 h over two days."""
    games, out = 100_000, []
    for i in range(9):  # -48 h .. now
        at = NOW - (48 - 6 * i) * H
        out.append((at, games))
        games += (per_day_before if at < NOW - 24 * H else per_day_now) // 4
    return out


def test_usage_rates_and_surge() -> None:
    current, previous = rs.usage_rates(usage(4_000, 10_000), NOW, 24)
    assert previous is not None and current is not None
    assert previous == 4_000
    assert current == 10_000
    signal = rs.usage_surge(current, previous, cohort_median=3_000, cfg=CFG)
    assert signal is not None
    assert "10.000 Spiele/Tag statt 4.000 (Faktor 2,5)" in signal.reasons[0]
    assert "3,3-mal so viel wie der Schnitt" in signal.reasons[1]


def test_no_usage_surge_without_acceleration_or_history() -> None:
    current, previous = rs.usage_rates(usage(8_000, 8_000), NOW, 24)
    assert rs.usage_surge(current, previous, 3_000, CFG) is None
    assert rs.usage_surge(10_000, None, 3_000, CFG) is None
    assert rs.usage_surge(400, 100, None, CFG) is None  # too few games to matter
    assert rs.usage_surge(10_000, 4_000, 20_000, CFG) is None  # below the typical card
    assert rs.usage_rates([(NOW, 1)], NOW, 24) == (None, None)
    assert rs.cohort_median([None, 5.0, 1.0]) is None
    assert rs.cohort_median([1.0, 2.0, 3.0, None]) == 2.0


# --- FODDER_RISE / potential -------------------------------------------------------------------


def test_fodder_rise() -> None:
    history = {
        86: [(NOW - 30 * H, 3_500), (NOW - 24 * H, 3_500), (NOW, 4_200)],
        87: [(NOW - 24 * H, 6_000), (NOW, 6_100)],
        88: [(NOW, 9_500)],
    }
    lines = {line.rating: line for line in rs.fodder_lines(history, NOW, CFG)}
    assert lines[86].change_pct == 20.0
    assert lines[86].signal is not None
    assert "86er +20,0 % in 24 h → 4.200" in lines[86].signal.reasons[0]
    assert lines[87].signal is None  # +1.7 %
    assert lines[88].change_pct is None  # no history yet


def test_potential_combines_signals() -> None:
    a = rs.RadarSignal(rs.RadarKind.TREND_START, 70, ())
    b = rs.RadarSignal(rs.RadarKind.USAGE_SURGE, 60, ())
    assert rs.potential([]) == 0
    assert rs.potential([a]) == 70
    assert rs.potential([a, b]) == 75


# --- scanner in the collector -------------------------------------------------------------------

LIST = (
    '<a href="/27/player/21487/maradona">x</a><a href="/27/player/21516/muller">x</a>'
    '<a href="/27/player/22947/michael-olise">x</a>'
)
PAGES = {
    "/robots.txt": "User-agent: *\nDisallow: /*?*\n",
    "/27/popular": LIST,
    "/27/latest": '<a href="/27/player/21487/maradona">x</a>',
    "/27/squad-building-challenges/cheapest": fixture("cheapest.html"),
    "/27/player/21487/maradona": fixture("21487-maradona.html"),
    "/27/player/21516/muller": fixture("21516-muller.html"),
    "/27/player/22947/michael-olise": fixture("22947-olise-totw.html"),
}


def scanner(tmp_path: Path, requests: list[str], **overrides: object) -> Collector:
    settings = Settings(
        _env_file=None,
        db_path=tmp_path / "fcast.db",
        platform="pc",
        sources="",
        radar_resolve_per_run=2,
        **overrides,  # type: ignore[arg-type]
    )
    collector = Collector(settings, sources=[])

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        text = PAGES.get(request.url.path)
        return httpx.Response(200, text=text) if text else httpx.Response(404)

    async def no_sleep(_: float) -> None:
        return None

    client = PoliteHttpClient(
        "FCast/test", transport=httpx.MockTransport(handler), sleep=no_sleep, cache_ttl=0
    )

    def lookup(ea_id: int) -> str | None:
        return collector._lookup_ref(ea_id, "futbin")

    collector.sources.append(FutbinSource(client, lookup, Platform.PC))
    return collector


async def test_scanner_reads_lists_resolves_and_prices_radar_cards(tmp_path: Path) -> None:
    requests: list[str] = []
    collector = scanner(tmp_path, requests)
    try:
        await collector.run_once()
        with collector.session_factory() as session:
            cards = {c.futbin_ref: c for c in session.scalars(select(RadarCard))}
            assert set(cards) == {
                "/27/player/21487/maradona",
                "/27/player/21516/muller",
                "/27/player/22947/michael-olise",
            }
            assert cards["/27/player/21487/maradona"].list_name == "popular"
            resolved = [c for c in cards.values() if c.player_id is not None]
            assert len(resolved) == 2  # at most two lookups per run
            assert all(c.checked_at is not None for c in resolved)  # priced in the same run
            olise = repo.get_player_by_ea_id(session, 50579475)
            if olise is not None:  # not resolved yet in run 1 (order by first seen)
                assert repo.latest_snapshot(session, olise, Platform.PC) is None
            fodder = session.scalars(select(FodderPrice)).all()
            assert {f.rating for f in fodder} == set(range(82, 91))
            assert next(f.price for f in fodder if f.rating == 85) == 1_867  # Ø of 1.8/1.9/1.9K
            for card in resolved:
                assert card.player is not None
                assert repo.latest_snapshot(session, card.player, Platform.PC) is not None
                assert repo.list_usage(session, card.player)  # games counter recorded

        lists_before = sum(1 for r in requests if r in ("/27/popular", "/27/latest"))
        await collector.run_once()  # lists and fodder are not due again; the third card is
        assert sum(1 for r in requests if r in ("/27/popular", "/27/latest")) == lists_before
        assert requests.count("/27/squad-building-challenges/cheapest") == 1
        with collector.session_factory() as session:
            assert all(c.player_id for c in session.scalars(select(RadarCard)))
    finally:
        await collector.aclose()


async def test_scanner_can_be_switched_off(tmp_path: Path) -> None:
    requests: list[str] = []
    collector = scanner(tmp_path, requests, radar_per_run=0)
    try:
        await collector.run_once()
        assert "/27/popular" not in requests
    finally:
        await collector.aclose()


def test_hits_show_radar_cards_with_early_signals(tmp_path: Path) -> None:
    collector = scanner(tmp_path, [])
    now = NOW
    with collector.session_factory.begin() as session:
        rising = repo.upsert_player(session, 1, repo.PlayerDetails(name="Steigt", rating=85))
        flat = repo.upsert_player(session, 2, repo.PlayerDetails(name="Flach", rating=85))
        for player in (rising, flat):
            session.add(
                RadarCard(
                    futbin_ref=f"/27/player/{player.ea_id}/x",
                    list_name="popular",
                    player_id=player.id,
                    first_seen_at=now,
                    last_seen_at=now,
                )
            )
        prices = [20_000] * 6 + [20_000, 20_250, 20_500, 20_750, 21_000, 21_500]
        for at, price in hourly(prices):
            repo.add_snapshot(session, rising, Platform.PC, price, "futbin", at)
            repo.add_snapshot(session, flat, Platform.PC, 20_000, "futbin", at)
        found = radar.hits(session, collector.settings, now)
    assert [h.player.ea_id for h in found] == [1]
    assert found[0].signals[0].kind is rs.RadarKind.TREND_START
    assert found[0].list_name == "popular"
    assert not found[0].on_watchlist
