"""Leak & promo radar: feeds, entity detection, link scoring, pool, candidates (no live HTTP)."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from fcast.analysis.signals import Rule
from fcast.analysis.stats import price_stats
from fcast.config import Platform, Settings
from fcast.db import repositories as repo
from fcast.db.base import Base
from fcast.db.models import LeakItem, LinkType, PoolCard
from fcast.db.session import create_db_engine, create_session_factory
from fcast.promos import service
from fcast.promos.entities import detect, display_name
from fcast.promos.feeds import is_relevant, parse_feed_setting, parse_rss
from fcast.promos.scoring import (
    CardInfo,
    PromoInfo,
    PromoWeights,
    best_matches,
    is_prebuy,
    link_strength,
    price_factor,
    score_card,
    timing_factor,
)
from fcast.sources.base import SourceError
from fcast.sources.futbin import FutbinSource
from fcast.sources.http import PoliteHttpClient

FIXTURES = Path(__file__).parent / "fixtures"
REALSPORT = (FIXTURES / "feeds" / "realsport101.xml").read_text(encoding="utf-8")
FIFAUTEAM = (FIXTURES / "feeds" / "fifauteam.xml").read_text(encoding="utf-8")
OLISE = (FIXTURES / "futbin" / "22947-olise-totw.html").read_text(encoding="utf-8")
NOW = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)
W = PromoWeights()


def settings(**overrides: object) -> Settings:
    values: dict[str, object] = {"platform": "pc", "sources": ""}
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


# --- feeds ---------------------------------------------------------------------------


def test_parse_realsport_feed() -> None:
    items = parse_rss(REALSPORT, "realsport101")
    assert len(items) == 12
    relevant = [i for i in items if is_relevant(i)]
    leaks = [i for i in relevant if i.is_leak]
    assert any("Team of the Week 3 Leaked" in i.title for i in leaks)
    assert all(i.published is not None and i.published.tzinfo is UTC for i in items)
    # Non-FC articles are filtered out.
    assert not any("MLB" in i.title for i in relevant)


def test_parse_fifauteam_feed() -> None:
    relevant = [i for i in parse_rss(FIFAUTEAM, "fifauteam") if is_relevant(i)]
    assert [i.title for i in relevant] == ["FC 27 Future Stars Event"]


def test_invalid_feed() -> None:
    with pytest.raises(SourceError):
        parse_rss("<rss><channel><item>", "broken")


def test_xml_bombs_are_rejected() -> None:
    bomb = (
        '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">'
        '<!ENTITY lol2 "&lol;&lol;&lol;&lol;">]><rss><channel><item><title>&lol2;</title>'
        "</item></channel></rss>"
    )
    with pytest.raises(Exception, match=r"(?i)entit|forbidden"):
        parse_rss(bomb, "evil")


def test_parse_feed_setting() -> None:
    assert parse_feed_setting("a=https://a.example/feed, b=http://b, broken, c=ftp://x") == [
        ("a", "https://a.example/feed"),
        ("b", "http://b"),
    ]


# --- entity detection ------------------------------------------------------------------

SLUGS = [
    "martin-odegaard",
    "temwa-chawinga",
    "lynn-williams-chawinga",
    "erling-haaland",
    "harry-kane",
    "rodri",
    "kobbie-mainoo",
    "lamine-yamal-nasraoui-ebana",
]


def test_detect_full_names_and_surnames() -> None:
    found = detect("TOTW 3 Leaked: Ødegaard & Chawinga lead; Erling Haaland headlines", SLUGS)
    assert found.players[0] == "erling-haaland"
    assert "martin-odegaard" in found.players
    assert {"temwa-chawinga", "lynn-williams-chawinga"} <= set(found.players)


def test_detect_ignores_lowercase_words_and_ambiguous_surnames() -> None:
    slugs = [*SLUGS, "a-silva", "b-silva", "c-silva", "d-silva"]
    assert detect("they lead the squad with rodri-like mainoo", slugs).players == []
    assert detect("Silva scores again", slugs).players == []  # four players named Silva


def test_detect_leagues_nations_clubs() -> None:
    found = detect(
        "Premier League & LaLiga stars, Brazilian and Spanish players from Arsenal and Barça",
        [],
        known_clubs=["1. FSV Mainz 05"],
    )
    assert found.leagues == ["Premier League", "LALIGA EA SPORTS"]
    assert found.nations == ["Brazil", "Spain"]
    assert found.clubs == ["FC Barcelona", "Arsenal"]
    assert detect("nothing here", []).empty


def test_display_name() -> None:
    assert display_name("martin-odegaard") == "Martin Odegaard"


# --- link scoring ---------------------------------------------------------------------


def promo(days: float = 4, confidence: float = 0.8, **links: str) -> PromoInfo:
    return PromoInfo(
        promo_id=1,
        name="Future Stars",
        starts_at=NOW + timedelta(days=days),
        ends_at=NOW + timedelta(days=days + 7),
        confidence=confidence,
        links=tuple(links.items()),
    )


CARD = CardInfo(
    ea_id=1,
    name="Michael Olise",
    slug="michael-olise",
    club="FC Bayern München",
    league="Bundesliga",
    nation="France",
)


@pytest.mark.parametrize(
    ("links", "strength", "reason"),
    [
        ({"player": "michael-olise"}, 1.0, "ist selbst im Leak"),
        ({"club": "FC Bayern Munich"}, 0.6, "gleicher Verein"),
        ({"league": "Bundesliga"}, 0.35, "gleiche Liga"),
        ({"nation": "France"}, 0.3, "gleiche Nation"),
        ({"league": "Bundesliga", "nation": "France"}, 0.45, "gleiche Liga"),
        ({"club": "Arsenal"}, 0.0, None),
    ],
)
def test_link_strength(links: dict[str, str], strength: float, reason: str | None) -> None:
    value, reasons = link_strength(CARD, promo(**links), W)
    assert value == pytest.approx(strength)
    if reason:
        assert reasons[0].startswith(reason)
    else:
        assert reasons == []


@pytest.mark.parametrize(
    ("days", "factor"),
    [(4, 1.0), (2, 1.0), (7, 1.0), (1, 0.7), (10, 0.6), (20, 0.0), (-1, 0.4), (-9, 0.0)],
)
def test_timing_factor(days: float, factor: float) -> None:
    assert timing_factor(promo(days=days), NOW)[0] == factor


def test_price_factor_prefers_cheap_cards() -> None:
    cheap = price_stats(
        [(NOW - timedelta(hours=h), 10_000) for h in range(1, 100)] + [(NOW, 8_000)], NOW
    )
    expensive = price_stats(
        [(NOW - timedelta(hours=h), 10_000) for h in range(1, 100)] + [(NOW, 12_000)], NOW
    )
    assert price_factor(cheap) > price_factor(None) > price_factor(expensive)


def test_score_and_prebuy() -> None:
    match = score_card(CARD, promo(days=4, player="michael-olise"), None, NOW, W)
    assert match is not None
    # 100 * strength 1.0 * timing 1.0 * confidence 0.8 * price 0.8 * liquidity 0.7
    assert match.score == pytest.approx(44.8)
    assert is_prebuy(match, PromoWeights(threshold=40))
    assert not is_prebuy(match, PromoWeights(threshold=50))
    late = score_card(CARD, promo(days=1, player="michael-olise"), None, NOW, W)
    assert late is not None
    assert not is_prebuy(late, PromoWeights(threshold=0))  # starts in less than 2 days
    assert score_card(CARD, promo(days=4, club="Arsenal"), None, NOW, W) is None


def test_best_matches_picks_strongest_promo_per_card() -> None:
    weak = promo(days=4, nation="France")
    strong = PromoInfo(
        **{**weak.__dict__, "promo_id": 2, "name": "TOTW", "links": (("player", "michael-olise"),)}
    )
    (best,) = best_matches([(CARD, None)], [weak, strong], NOW, W)
    assert best.promo.name == "TOTW"


def test_weights_from_settings() -> None:
    weights = PromoWeights.from_settings(
        settings(promo_weight_league=0.5, promo_prebuy_threshold=70)
    )
    assert weights.league == 0.5
    assert weights.threshold == 70


# --- service -------------------------------------------------------------------------


@pytest.fixture
def factory() -> sessionmaker[Session]:
    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def mock_client(pages: dict[str, str], requests: list[str] | None = None) -> PoliteHttpClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /*?*\n")
        text = pages.get(request.url.path)
        return httpx.Response(200, text=text) if text is not None else httpx.Response(404)

    async def no_sleep(_: float) -> None:
        return None

    return PoliteHttpClient("FCast/test", transport=httpx.MockTransport(handler), sleep=no_sleep)


async def test_sync_feeds_stores_new_relevant_items_once(factory: sessionmaker[Session]) -> None:
    client = mock_client({"/feed.xml": REALSPORT})
    config = settings(leak_feeds="realsport101=https://realsport101.com/feed.xml")
    first = await service.sync_feeds(client, factory, config)
    second = await service.sync_feeds(client, factory, config)
    await client.aclose()
    assert first >= 5
    assert second == 0
    with factory() as session:
        inbox = service.inbox(session)
    assert inbox[0].published_at is not None
    assert any(item.is_leak for item in inbox)


def test_create_promo_marks_leak_as_used(factory: sessionmaker[Session]) -> None:
    with factory.begin() as session:
        leak = LeakItem(source="x", guid="1", title="Leak", url="https://x/1")
        session.add(leak)
        session.flush()
        promo_row = service.create_promo(
            session, "Future Stars", NOW + timedelta(days=3), None, 0.7, None, None,
            [(LinkType.PLAYER, "michael-olise"), (LinkType.LEAGUE, "Bundesliga")],
            leak_id=leak.id,
        )  # fmt: skip
        assert {(link.link_type, link.link_value) for link in promo_row.links} == {
            (LinkType.PLAYER, "michael-olise"),
            (LinkType.LEAGUE, "Bundesliga"),
        }
        assert leak.status == "used"
        assert leak.promo_id == promo_row.id


SITEMAPS = {
    "/sitemap_index.xml": "<sitemapindex><sitemap><loc>https://www.futbin.com/27/player/0/"
    "sitemap.xml</loc></sitemap></sitemapindex>",
    "/27/player/0/sitemap.xml": "<urlset><url><loc>https://www.futbin.com/27/player/22947/"
    "michael-olise</loc></url></urlset>",
    "/27/player/22947/michael-olise": OLISE,
}


async def test_fill_pool_tracks_base_cards_of_leaked_players(
    factory: sessionmaker[Session],
) -> None:
    with factory.begin() as session:
        promo_id = service.create_promo(
            session, "Future Stars", NOW + timedelta(days=3), NOW + timedelta(days=10), 0.7,
            None, None, [(LinkType.PLAYER, "michael-olise"), (LinkType.PLAYER, "nobody-known")],
        ).id  # fmt: skip
    futbin = FutbinSource(mock_client(SITEMAPS), lambda _: None, Platform.PC)
    added = await service.fill_pool(futbin, factory, promo_id, settings())
    await futbin.aclose()
    assert added == [50579475]
    with factory() as session:
        pool = session.scalars(select(PoolCard)).one()
        assert pool.player.name == "Michael Olise"
        assert pool.player.club == "FC Bayern München"
        assert pool.until == NOW + timedelta(days=13)  # promo end + 3 days
        assert repo.get_source_ref(session, 50579475, "futbin") == "/27/player/22947/michael-olise"


def test_due_pool_cards_respects_interval_limit_and_watchlist(
    factory: sessionmaker[Session],
) -> None:
    config = settings(pool_interval_h=12, pool_per_run=2)
    with factory.begin() as session:
        for ea_id, hours_ago in [(1, 20), (2, 30), (3, 2), (4, 40)]:
            player = repo.upsert_player(session, ea_id)
            session.add(PoolCard(player_id=player.id, reason="x", until=NOW + timedelta(days=5)))
            repo.add_snapshot(
                session, player, Platform.PC, 1000, "futbin", NOW - timedelta(hours=hours_ago)
            )
        repo.set_watch(session, repo.upsert_player(session, 4))  # watched: priced anyway
        expired = repo.upsert_player(session, 5)
        session.add(PoolCard(player_id=expired.id, reason="x", until=NOW - timedelta(days=1)))
    with factory() as session:
        # Oldest first, at most two per run; card 3 is fresh, 4 is watched, 5 expired.
        assert service.due_pool_cards(session, NOW, config) == [2, 1]


def test_candidates_and_prebuy_signal(factory: sessionmaker[Session]) -> None:
    config = settings(promo_prebuy_threshold=30)
    with factory.begin() as session:
        olise_details = repo.PlayerDetails(
            name="Michael Olise", club="FC Bayern München", league="Bundesliga", nation="France"
        )
        olise = repo.upsert_player(session, 50579475, olise_details)
        repo.set_source_ref(session, olise, "futbin", "/27/player/22947/michael-olise")
        session.add(PoolCard(player_id=olise.id, reason="x", until=NOW + timedelta(days=9)))
        other_details = repo.PlayerDetails(
            name="Other", club="Arsenal", league="Premier League", nation="France"
        )
        other = repo.upsert_player(session, 2, other_details)
        repo.set_watch(session, other)
        for hours in range(1, 60):
            repo.add_snapshot(
                session, olise, Platform.PC, 100_000, "futbin", NOW - timedelta(hours=hours)
            )
        repo.add_snapshot(session, olise, Platform.PC, 90_000, "futbin", NOW)
        service.create_promo(
            session, "Future Stars", NOW + timedelta(days=4), None, 0.9, None, None,
            [(LinkType.PLAYER, "michael-olise"), (LinkType.NATION, "France")],
        )  # fmt: skip
        service.create_promo(session, "Far away", NOW + timedelta(days=30), None, 0.9, None, None,
                             [(LinkType.NATION, "France")])  # fmt: skip
    with factory() as session:
        found = service.candidates(session, config, NOW)
        assert [c.player.name for c in found] == ["Michael Olise", "Other"]
        top = found[0]
        assert top.in_pool
        assert top.price == 90_000
        assert "ist selbst im Leak" in top.match.reasons
        assert found[1].on_watchlist
        assert "gleiche Nation" in found[1].match.reasons[0]
        signals = service.prebuy_signals(session, config, NOW)
    assert [s.name for s in signals] == ["Michael Olise"]
    assert signals[0].rule is Rule.PROMO_PREBUY
    assert "Future Stars startet in ~4 Tagen" in signals[0].reasons


def test_no_candidates_without_promos(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        assert service.candidates(session, settings(), NOW) == []
