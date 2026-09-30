"""Holo pairs: detection on FUTBIN pages, pairing, pricing cadence and the HOLO_SPREAD signal."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy.orm import Session, sessionmaker

from fcast.analysis import signals as sig
from fcast.analysis.service import analyze_player
from fcast.analysis.stats import price_stats
from fcast.config import Platform, Settings
from fcast.db import repositories as repo
from fcast.db.base import Base
from fcast.db.models import CardPair
from fcast.db.session import create_db_engine, create_session_factory
from fcast.holo import discover_pairs, due_holo_cards, holo_partner, normal_of
from fcast.sources.futbin import (
    HOLO_ID_OFFSET,
    FutbinSource,
    is_holo_page,
    parse_card_id,
    parse_versions,
)
from fcast.sources.http import PoliteHttpClient

FIXTURES = Path(__file__).parent / "fixtures" / "futbin"
NORMAL = (FIXTURES / "22947-olise-totw.html").read_text(encoding="utf-8")
HOLO = (FIXTURES / "22948-olise-totw-holo.html").read_text(encoding="utf-8")
MARADONA = (FIXTURES / "21487-maradona.html").read_text(encoding="utf-8")
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
CFG = sig.SignalConfig()


# --- FUTBIN pages ----------------------------------------------------------------------


def test_holo_detection_and_versions() -> None:
    assert not is_holo_page(NORMAL)
    assert is_holo_page(HOLO)
    versions = parse_versions(NORMAL)
    holo = [v for v in versions if v.holo]
    assert [(v.futbin_id, v.rating) for v in holo] == [(22948, 91)]
    assert any(v.futbin_id == 62 and not v.holo for v in versions)
    # Maradona's second FUTBIN page is the holo of his icon card.
    assert [v.futbin_id for v in parse_versions(MARADONA) if v.holo] == [21489]


def test_holo_id_is_normal_id_plus_two_to_the_24th() -> None:
    assert parse_card_id(HOLO) == parse_card_id(NORMAL) + HOLO_ID_OFFSET  # type: ignore[operator]


# --- signal --------------------------------------------------------------------------------


def stats_at(price: int) -> sig.PriceStats:  # type: ignore[name-defined]
    return price_stats([(NOW - timedelta(hours=h), price) for h in range(0, 30)], NOW)


def test_holo_spread_signal() -> None:
    holo = sig.HoloQuote(price=2_499_000, extinct=False)
    supply = sig.Supply(listings=(1_080_000, 1_084_000), range_max=10_000_000)
    signal = sig.holo_spread(1, "Olise", stats_at(1_080_000), holo, supply, CFG)
    assert signal is not None
    assert signal.rule is sig.Rule.HOLO_SPREAD
    assert signal.recommended == 2_498_000  # one step below the holo
    assert signal.expected_profit == 2_373_100 - 1_080_000
    assert signal.score == 100.0  # spread +131 %, capped
    assert "normale Karte knapp (2 Angebote)" in signal.reasons


def test_holo_spread_below_threshold_or_unprofitable() -> None:
    small = sig.HoloQuote(price=1_200_000, extinct=False)  # +11 %
    assert sig.holo_spread(1, "X", stats_at(1_080_000), small, None, CFG) is None
    no_price = sig.HoloQuote(price=None, extinct=True)
    assert sig.holo_spread(1, "X", stats_at(1_080_000), no_price, None, CFG) is None
    capped = sig.Supply(listings=(1_000,), range_max=1_000)
    big = sig.HoloQuote(price=5_000, extinct=False)
    assert sig.holo_spread(1, "X", stats_at(1_000), big, capped, CFG) is None  # EA max = price


def test_extinct_holo_uses_last_known_price() -> None:
    holo = sig.HoloQuote(price=3_000_000, extinct=True, stale=True)
    signal = sig.holo_spread(1, "Olise", stats_at(1_380_000), holo, None, CFG)
    assert signal is not None
    assert "Holo gerade extinct (letzter bekannter Preis)" in signal.reasons


# --- pairing -----------------------------------------------------------------------------


@pytest.fixture
def factory() -> sessionmaker[Session]:
    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def futbin_source(requests: list[str], pages: dict[str, str] | None = None) -> FutbinSource:
    served = pages or {
        "/27/player/22947/michael-olise": NORMAL,
        "/27/player/22948/michael-olise": HOLO,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /*?*\n")
        text = served.get(request.url.path)
        return httpx.Response(200, text=text) if text else httpx.Response(404)

    async def no_sleep(_: float) -> None:
        return None

    client = PoliteHttpClient("FCast/test", transport=httpx.MockTransport(handler), sleep=no_sleep)
    return FutbinSource(client, lambda _: None, Platform.PC)


def watch_olise(factory: sessionmaker[Session]) -> None:
    with factory.begin() as session:
        player = repo.upsert_player(
            session, 50579475, repo.PlayerDetails(name="Michael Olise", rating=91)
        )
        repo.set_watch(session, player)
        repo.set_source_ref(session, player, "futbin", "/27/player/22947/michael-olise")


def settings() -> Settings:
    return Settings(_env_file=None, platform="pc", sources="", holo_interval_h=2)


async def test_discover_pairs_links_holo_version(factory: sessionmaker[Session]) -> None:
    watch_olise(factory)
    requests: list[str] = []
    source = futbin_source(requests)
    assert await discover_pairs(source, factory, settings(), NOW) == [50579475]
    # Already paired: no new lookup.
    assert await discover_pairs(source, factory, settings(), NOW + timedelta(hours=1)) == []
    await source.aclose()
    assert requests.count("/27/player/22948/michael-olise") == 1

    with factory() as session:
        olise = repo.get_player_by_ea_id(session, 50579475)
        assert olise is not None
        holo = holo_partner(session, olise)
        assert holo is not None
        assert holo.ea_id == 67356691
        assert holo.card_type == "Team of the Week (Holo)"
        assert repo.get_source_ref(session, 67356691, "futbin") == "/27/player/22948/michael-olise"
        assert normal_of(session, holo) == olise


async def test_cards_without_holo_are_rechecked_later(factory: sessionmaker[Session]) -> None:
    watch_olise(factory)
    requests: list[str] = []
    no_holo_page = NORMAL.replace("player-rating-card-holo", "player-rating-card-plain")
    source = futbin_source(requests, {"/27/player/22947/michael-olise": no_holo_page})
    assert await discover_pairs(source, factory, settings(), NOW) == []
    before = len(requests)
    await discover_pairs(source, factory, settings(), NOW + timedelta(hours=5))
    assert len(requests) == before  # checked recently
    await discover_pairs(source, factory, settings(), NOW + timedelta(days=3))
    await source.aclose()
    with factory() as session:
        olise = repo.get_player_by_ea_id(session, 50579475)
        assert olise is not None
        pair = session.get(CardPair, olise.id)
        assert pair is not None
        # Checked again after two days (the page itself came from the cache).
        assert pair.checked_at == NOW + timedelta(days=3)
        assert pair.holo_player_id is None


async def test_holo_partner_is_priced_every_interval(factory: sessionmaker[Session]) -> None:
    watch_olise(factory)
    source = futbin_source([])
    await discover_pairs(source, factory, settings(), NOW)
    await source.aclose()
    with factory.begin() as session:
        assert due_holo_cards(session, NOW, settings()) == [67356691]  # never priced
        holo = repo.get_player_by_ea_id(session, 67356691)
        assert holo is not None
        repo.add_snapshot(session, holo, Platform.PC, 2_000_000, "futbin", NOW - timedelta(hours=1))
        assert due_holo_cards(session, NOW, settings()) == []
        assert due_holo_cards(session, NOW + timedelta(hours=2), settings()) == [67356691]


async def test_analysis_shows_holo_and_signal(factory: sessionmaker[Session]) -> None:
    watch_olise(factory)
    source = futbin_source([])
    await discover_pairs(source, factory, settings(), NOW)
    await source.aclose()
    with factory.begin() as session:
        olise = repo.get_player_by_ea_id(session, 50579475)
        holo = repo.get_player_by_ea_id(session, 67356691)
        assert olise is not None and holo is not None
        for hours in range(0, 24):
            at = NOW - timedelta(hours=hours)
            repo.add_snapshot(session, olise, Platform.PC, 1_380_000, "futbin", at)
        repo.add_snapshot(session, holo, Platform.PC, 2_499_000, "futbin", NOW)
        result = analyze_player(session, olise, settings(), NOW)
    assert result.holo_player is not None
    assert result.holo_spread_pct == pytest.approx(81.1, abs=0.1)
    rules = [s.rule for s in result.signals]
    assert sig.Rule.HOLO_SPREAD in rules
