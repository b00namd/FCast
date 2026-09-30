"""TOTW prediction: calendar, OpenLigaDB parsing, name matching, scoring, full run (mocked)."""

import json
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from fcast.alerts.notifier import Notification, Notifier
from fcast.config import Platform, Settings
from fcast.db.base import Base
from fcast.db.models import Player, PoolCard, TotwActual, TotwPrediction
from fcast.db.session import create_db_engine, create_session_factory
from fcast.sources.futbin import FutbinSource
from fcast.sources.http import PoliteHttpClient
from fcast.totw import calendar
from fcast.totw.futbin_totw import parse_totw_page
from fcast.totw.names import clubs_match, matches_slug, name_parts
from fcast.totw.openligadb import OpenLigaDbClient, parse_matches
from fcast.totw.scoring import TotwWeights, score_candidates
from fcast.totw.service import TotwService, current_season, hit_rate

FIXTURES = Path(__file__).parent / "fixtures"
MATCHDAY_4 = json.loads((FIXTURES / "openligadb" / "bl1-2026-4.json").read_text(encoding="utf-8"))
TOTW_2 = (FIXTURES / "futbin" / "totw-2.html").read_text(encoding="utf-8")
OLISE = (FIXTURES / "futbin" / "22947-olise-totw.html").read_text(encoding="utf-8")
BERLIN = ZoneInfo("Europe/Berlin")
FIRST = date(2026, 9, 16)
SEVEN_PM = time(19, 0)


# --- calendar -------------------------------------------------------------------


def test_weeks_follow_wednesday_releases() -> None:
    week2 = calendar.week(FIRST, SEVEN_PM, BERLIN, 2)
    assert week2.release == datetime(2026, 9, 23, 17, 0, tzinfo=UTC)  # 19:00 CEST
    assert week2.window_start == datetime(2026, 9, 16, 17, 0, tzinfo=UTC)
    before = datetime(2026, 9, 23, 16, 59, tzinfo=UTC)
    assert calendar.upcoming_week(before, FIRST, SEVEN_PM, BERLIN).number == 2
    after = datetime(2026, 9, 23, 17, 0, tzinfo=UTC)
    assert calendar.upcoming_week(after, FIRST, SEVEN_PM, BERLIN).number == 3
    released = calendar.last_released_week(after, FIRST, SEVEN_PM, BERLIN)
    assert released is not None
    assert released.number == 2
    assert (
        calendar.last_released_week(datetime(2026, 9, 1, tzinfo=UTC), FIRST, SEVEN_PM, BERLIN)
        is None
    )


def test_current_season() -> None:
    assert current_season(datetime(2026, 9, 30, tzinfo=UTC)) == 2026
    assert current_season(datetime(2027, 3, 1, tzinfo=UTC)) == 2026


# --- OpenLigaDB --------------------------------------------------------------------


def test_parse_matches() -> None:
    matches = parse_matches(MATCHDAY_4, "bl1")
    assert len(matches) == 9
    first = matches[0]
    assert (first.home, first.away, first.home_goals, first.away_goals) == (
        "Bayer 04 Leverkusen",
        "RB Leipzig",
        2,
        0,
    )
    assert first.kickoff == datetime(2026, 9, 20, 13, 30, tzinfo=UTC)
    assert first.goals[0].scorer == "P. Schick"
    assert first.result_for(first.home_id) == "win"
    assert first.result_for(first.away_id) == "loss"
    assert first.result_for(999) is None


# --- names ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "slug", "fits"),
    [
        ("P. Schick", "patrik-schick", True),
        ("M. Olise", "michael-olise", True),
        ("Miguel Gutiérrez", "miguel-gutierrez-ortega", True),
        ("Pascal Groß", "pascal-gro", True),
        ("H. Kane", "harry-kane", True),
        ("H. Kane", "harry-maguire", False),
        ("M. Olise", "nathan-olise", False),  # initial does not fit
        ("Kane", "harry-kane", True),
        ("", "harry-kane", False),
    ],
)
def test_matches_slug(name: str, slug: str, fits: bool) -> None:
    assert matches_slug(name, slug) is fits


def test_name_parts() -> None:
    assert name_parts("P. Schick") == (["p"], ["schick"])
    assert name_parts("Younes Ebnoutalib") == ([], ["younes", "ebnoutalib"])


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [
        ("Bayer 04 Leverkusen", "Leverkusen", True),
        ("FC Bayern München", "FC Bayern Munich", True),
        ("Borussia Dortmund", "Borussia Mönchengladbach", False),
        ("1. FSV Mainz 05", "Mainz", True),
        ("SV Werder Bremen", None, False),
    ],
)
def test_clubs_match(a: str, b: str | None, same: bool) -> None:
    assert clubs_match(a, b) is same


# --- scoring ---------------------------------------------------------------------


def test_scoring_matchday_4() -> None:
    candidates = score_candidates(parse_matches(MATCHDAY_4, "bl1"), TotwWeights())
    top = candidates[0]
    assert top.name == "M. Olise"
    assert top.goals == 3
    assert "Hattrick" in top.reasons
    kane = next(c for c in candidates if c.name == "H. Kane")
    assert (kane.goals, kane.penalty_goals) == (2, 1)
    assert "Doppelpack" in kane.reasons
    assert "1 Elfer" in kane.reasons
    # Hattrick 3x3 + 3 + 2 + win 1 = 15; brace 3 + 2 (penalty) + 2 + 1 = 8
    assert (top.score, kane.score) == (15.0, 8.0)


def test_scoring_ignores_own_goals_and_unfinished_and_weighs_leagues() -> None:
    data = json.loads(json.dumps(MATCHDAY_4))
    data[0]["goals"][0]["isOwnGoal"] = True
    data[1]["matchIsFinished"] = False
    matches = parse_matches(data, "bl1")
    names = {c.name for c in score_candidates(matches, TotwWeights())}
    assert "P. Schick" not in names
    second_league = score_candidates(parse_matches(MATCHDAY_4, "bl2"), TotwWeights())
    assert second_league[0].score == pytest.approx(15.0 * 0.6)


def test_weights_from_settings() -> None:
    settings = Settings(_env_file=None, totw_weight_goal=5, totw_league_factors="bl1:1,bl2:0.5")
    weights = TotwWeights.from_settings(settings)
    assert weights.goal == 5
    assert weights.league_factor("bl2") == 0.5
    assert weights.league_factor("xyz") == 0.3


# --- FUTBIN TOTW page ---------------------------------------------------------------


def test_parse_totw_page() -> None:
    players = parse_totw_page(TOTW_2)
    assert len(players) == 23
    slugs = [p.slug for p in players]
    assert "michael-olise" in slugs
    assert "miguel-gutierrez-ortega" in slugs
    olise = players[slugs.index("michael-olise")]
    assert (olise.futbin_id, olise.name) == (22947, "Michael Olise")


# --- full run with mocked OpenLigaDB and FUTBIN ---------------------------------


class Recorder(Notifier):
    name = "recorder"

    def __init__(self) -> None:
        self.sent: list[Notification] = []

    async def send(self, notification: Notification) -> None:
        self.sent.append(notification)


SITEMAP_INDEX = (
    "<sitemapindex><sitemap><loc>https://www.futbin.com/27/player/0/sitemap.xml</loc>"
    "</sitemap></sitemapindex>"
)
SITEMAP = (
    "<urlset>"
    + "".join(
        f"<url><loc>https://www.futbin.com/27/player/{i}/{slug}</loc></url>"
        for i, slug in [(62, "michael-olise"), (22947, "michael-olise"), (40, "harry-kane")]
    )
    + "</urlset>"
)


def mock_client(handler: httpx.MockTransport) -> PoliteHttpClient:
    async def no_sleep(_: float) -> None:
        return None

    return PoliteHttpClient("FCast/test", transport=handler, sleep=no_sleep)


def openligadb_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/robots.txt":
        return httpx.Response(404)
    if path.startswith("/getcurrentgroup/"):
        return httpx.Response(200, json={"groupOrderID": 4, "groupName": "4. Spieltag"})
    if path == "/getmatchdata/bl1/2026/4":
        return httpx.Response(200, json=MATCHDAY_4)
    return httpx.Response(200, json=[])


def futbin_handler(requests: list[str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        requests.append(path)
        pages = {
            "/robots.txt": "User-agent: *\nDisallow: /*?*\n",
            "/sitemap_index.xml": SITEMAP_INDEX,
            "/27/player/0/sitemap.xml": SITEMAP,
            "/27/totw/TOTW2": TOTW_2,
        }
        if path in pages:
            return httpx.Response(200, text=pages[path])
        if path.startswith("/27/player/") and path.endswith("/michael-olise"):
            return httpx.Response(200, text=OLISE)  # club FC Bayern München
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.fixture
def factory() -> sessionmaker[Session]:
    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def make_service(
    factory: sessionmaker[Session], requests: list[str], notifier: Notifier | None = None
) -> TotwService:
    settings = Settings(_env_file=None, platform="pc", sources="", totw_leagues="bl1")
    futbin = FutbinSource(mock_client(futbin_handler(requests)), lambda _: None, Platform.PC)
    openligadb = OpenLigaDbClient(mock_client(httpx.MockTransport(openligadb_handler)))
    return TotwService(settings, factory, openligadb, futbin, notifier)


async def test_refresh_predicts_links_cards_and_evaluates(factory: sessionmaker[Session]) -> None:
    requests: list[str] = []
    notifier = Recorder()
    service = make_service(factory, requests, notifier)
    # Wednesday morning before TOTW 2 (10:00 local, inside the 20 h alert window, outside
    # quiet hours): matchday 4 (18.-20.09.) is in the window.
    tuesday = datetime(2026, 9, 23, 8, 0, tzinfo=UTC)
    report = await service.refresh(tuesday)
    assert report.week == 2
    assert report.matches == 9
    assert report.linked >= 1

    with factory() as session:
        predictions = session.scalars(
            select(TotwPrediction).where(TotwPrediction.week == 2).order_by(TotwPrediction.rank)
        ).all()
        top = predictions[0]
        assert (top.name, top.team) == ("M. Olise", "FC Bayern München")
        assert top.futbin_ref == "/27/player/62/michael-olise"  # oldest id first
        assert top.ea_id == 50579475
        assert top.price == 1_380_000  # PC price on the saved page
        assert top.chem_styles[0] == ("Hunter", 77)
        kane = next(p for p in predictions if p.name == "H. Kane")
        assert kane.futbin_ref is None  # club on the page does not match -> no card
        # The linked candidate is priced in the pool until 3 days after the release.
        olise = session.scalar(select(Player).where(Player.ea_id == 50579475))
        assert olise is not None
        pool = session.get(PoolCard, olise.id)
        assert pool is not None
        assert pool.reason == "TOTW 2 Kandidat"
        assert pool.until == service.upcoming(tuesday).release + timedelta(days=3)

    # Alert window (20 h before release): top candidates were sent once.
    assert len(notifier.sent) == 1
    assert notifier.sent[0].title.startswith("TOTW 2")
    assert "M. Olise" in notifier.sent[0].message
    await service.refresh(tuesday + timedelta(hours=1))
    assert len(notifier.sent) == 1

    # After the release the actual TOTW is read from FUTBIN and compared.
    thursday = datetime(2026, 9, 24, 10, 0, tzinfo=UTC)
    report = await service.refresh(thursday)
    assert report.week == 3
    assert report.evaluated_week == 2
    with factory() as session:
        assert len(session.scalars(select(TotwActual).where(TotwActual.week == 2)).all()) == 23
        rate = hit_rate(session, 2)
    assert rate is not None
    hits, total, names = rate
    assert "M. Olise" in names
    assert hits == len(names) >= 1
    assert total == 10
    # The linked card is cached: no second sitemap lookup for Olise.
    assert requests.count("/27/player/0/sitemap.xml") == 1


async def test_refresh_skips_futbin_while_paused(factory: sessionmaker[Session]) -> None:
    from fcast.db import repositories as repo

    requests: list[str] = []
    service = make_service(factory, requests)
    with factory.begin() as session:
        repo.pause_source(session, "futbin", datetime(2026, 9, 30, tzinfo=UTC), "HTTP 429")
    report = await service.refresh(datetime(2026, 9, 22, 18, 0, tzinfo=UTC))
    assert report.candidates > 0
    assert report.linked == 0
    assert requests == []


def test_hit_rate_without_prediction(factory: sessionmaker[Session]) -> None:
    with factory.begin() as session:
        session.add(TotwActual(week=5, futbin_id=1, slug="michael-olise", name="Michael Olise"))
    with factory() as session:
        assert hit_rate(session, 5) is None
        assert hit_rate(session, 6) is None
