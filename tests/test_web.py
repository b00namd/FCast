"""Dashboard route tests with FastAPI's TestClient (no scheduler, fake price source)."""

import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from fcast.collector.service import Collector
from fcast.config import Platform, Settings
from fcast.db import repositories as repo
from fcast.db.models import PriceSnapshot
from fcast.sources.base import PlayerInfo, PlayerNotFoundError, PriceQuote, PriceSource
from fcast.sources.futbin import FutbinSource
from fcast.sources.http import PoliteHttpClient
from fcast.web.app import create_app, format_coins, format_pct
from fcast.web.forms import NBSP, parse_coins

AUTH = ("fcast", "s3cret")
HTMX = {"HX-Request": "true"}


class StaticSource(PriceSource):
    name = "fake"
    remote = True

    def __init__(self, prices: dict[int, int]) -> None:
        self.prices = prices

    async def fetch_price(self, ea_id: int, platform: Platform) -> PriceQuote:
        if ea_id not in self.prices:
            raise PlayerNotFoundError(str(ea_id))
        return PriceQuote(ea_id, platform, self.prices[ea_id], self.name, datetime.now(UTC))

    async def fetch_player(self, ea_id: int) -> PlayerInfo:
        return PlayerInfo(ea_id=ea_id, name=f"Player {ea_id}", rating=90)


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "db_path": tmp_path / "fcast.db",
        "sources": "",
        "platform": "pc",
        "web_user": AUTH[0],
        "web_password": AUTH[1],
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


FUTBIN_PAGES = {
    "/27/player/21487/maradona": "21487-maradona.html",
    "/27/player/22947/michael-olise": "22947-olise-totw.html",
}
FUTBIN_REQUESTS: list[str] = []
FUTBIN_SITEMAPS = {
    "/sitemap_index.xml": "<sitemapindex><sitemap><loc>"
    "https://www.futbin.com/27/player/0/sitemap.xml</loc></sitemap></sitemapindex>",
    "/27/player/0/sitemap.xml": "<urlset>"
    "<url><loc>https://www.futbin.com/27/player/22947/michael-olise</loc></url>"
    "<url><loc>https://www.futbin.com/27/player/21487/maradona</loc></url></urlset>",
}


def fixture_futbin(collector: Collector) -> FutbinSource:
    """FUTBIN source that serves the saved pages from tests/fixtures/futbin (no network)."""
    fixtures = Path(__file__).parent / "fixtures" / "futbin"
    FUTBIN_REQUESTS.clear()

    def handler(request: httpx.Request) -> httpx.Response:
        FUTBIN_REQUESTS.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /*?*\n")
        if request.url.path in FUTBIN_SITEMAPS:
            return httpx.Response(200, text=FUTBIN_SITEMAPS[request.url.path])
        page = FUTBIN_PAGES.get(request.url.path)
        if page is None:
            return httpx.Response(404)
        return httpx.Response(200, text=(fixtures / page).read_text(encoding="utf-8"))

    async def no_sleep(_: float) -> None:
        return None

    client = PoliteHttpClient("FCast/test", transport=httpx.MockTransport(handler), sleep=no_sleep)
    return FutbinSource(client, lambda ea_id: collector._lookup_ref(ea_id, "futbin"), Platform.PC)


@pytest.fixture
def collector(tmp_path: Path) -> Collector:
    return Collector(make_settings(tmp_path), sources=[StaticSource({1: 1_000, 2: 2_000})])


@pytest.fixture
def client(tmp_path: Path, collector: Collector) -> Iterator[TestClient]:
    collector.sources.append(fixture_futbin(collector))
    app = create_app(make_settings(tmp_path), collector=collector, run_scheduler=False)
    with TestClient(app) as test_client:
        test_client.auth = AUTH
        yield test_client


def add_watch(collector: Collector, ea_id: int, **kwargs: int | None) -> None:
    with collector.session_factory.begin() as session:
        player = repo.upsert_player(session, ea_id, repo.PlayerDetails(name=f"P{ea_id}"))
        repo.set_watch(session, player, **kwargs)  # type: ignore[arg-type]


def add_price(
    collector: Collector, ea_id: int, price: int, at: datetime, source: str = "x"
) -> None:
    with collector.session_factory.begin() as session:
        player = repo.upsert_player(session, ea_id)
        repo.add_snapshot(session, player, Platform.PC, price, source, at)


# --- auth, health, security ------------------------------------------------


def test_health_is_public(client: TestClient) -> None:
    response = client.get("/health", auth=None)
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.parametrize("auth", [None, ("fcast", "wrong"), ("other", "s3cret")])
def test_pages_require_auth(client: TestClient, auth: tuple[str, str] | None) -> None:
    response = client.get("/prices", auth=auth)
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"].startswith("Basic")


def test_static_files_are_served(client: TestClient) -> None:
    assert client.get("/static/vendor/htmx.min.js", auth=None).status_code == 200
    assert client.get("/static/app.css", auth=None).status_code == 200


def test_app_requires_password(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="FCAST_WEB_PASSWORD"):
        create_app(make_settings(tmp_path, web_password=None), run_scheduler=False)


def test_cross_site_post_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/watchlist", data={"ea_id": "5"}, headers={"Origin": "https://evil.example"}
    )
    assert response.status_code == 403
    response = client.post(
        "/watchlist",
        data={"ea_id": "5"},
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_index_redirects_to_prices(client: TestClient) -> None:
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/prices"


# --- watchlist -------------------------------------------------------------


def test_watchlist_add_without_htmx(client: TestClient, collector: Collector) -> None:
    response = client.post(
        "/watchlist",
        data={
            "ea_id": "190042",
            "name": "Maradona",
            "buy": "5.500.000",
            "sell": "7,2M",
            "futbin_url": "https://www.futbin.com/27/player/21487/maradona",
            "note": "Icon",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = client.get("/watchlist").text
    assert "Maradona" in page
    assert "5.500.000" in page
    assert "7.200.000" in page
    assert "futbin.com/27/player/21487/maradona" in page
    with collector.session_factory() as session:
        assert repo.get_source_ref(session, 190042, "futbin") == "/27/player/21487/maradona"


def test_watchlist_add_with_htmx_returns_form_and_rows(client: TestClient) -> None:
    response = client.post("/watchlist", data={"ea_id": "231747", "buy": "4m"}, headers=HTMX)
    assert response.status_code == 200
    assert 'id="watch-form"' in response.text
    assert "gespeichert" in response.text
    assert 'hx-swap-oob="true"' in response.text
    assert "4.000.000" in response.text


@pytest.mark.parametrize(
    ("data", "error"),
    [
        ({"ea_id": "abc"}, "EA-ID"),
        ({"ea_id": "1", "buy": "viel"}, "Ziel-Kaufpreis"),
        ({"ea_id": "1", "futbin_url": "https://www.fut.gg/players/1"}, "FUTBIN-Link"),
    ],
)
def test_watchlist_add_validation(client: TestClient, data: dict[str, str], error: str) -> None:
    response = client.post("/watchlist", data=data, headers=HTMX)
    assert response.status_code == 200
    assert error in response.text
    assert "hx-swap-oob" not in response.text
    assert client.post("/watchlist", data=data).status_code == 422


def test_watchlist_tax_warning(client: TestClient) -> None:
    response = client.post(
        "/watchlist", data={"ea_id": "1", "buy": "10000", "sell": "10200"}, headers=HTMX
    )
    assert "5 % Steuer" in response.text


def test_watchlist_edit_update_and_toggle(client: TestClient, collector: Collector) -> None:
    add_watch(collector, 7, target_buy=1_000)
    edit = client.get("/watchlist/7/edit")
    assert edit.status_code == 200
    assert 'name="buy"' in edit.text

    updated = client.post(
        "/watchlist/7",
        data={"name": "Neu", "buy": "900", "sell": "1500", "futbin_url": "", "note": "x"},
    )
    assert updated.status_code == 200
    assert "Neu" in updated.text
    assert "1.500" in updated.text

    bad = client.post("/watchlist/7", data={"buy": "nope"})
    assert "ungültiger Betrag" in bad.text

    toggled = client.post("/watchlist/7/toggle")
    assert "inaktiv" in toggled.text
    assert "Aktivieren" in toggled.text
    # Editing an inactive entry keeps it inactive.
    client.post("/watchlist/7", data={"buy": "800"})
    with collector.session_factory() as session:
        player = repo.get_player_by_ea_id(session, 7)
        assert player is not None
        entry = repo.get_watch(session, player)
        assert entry is not None
        assert entry.active is False
        assert entry.target_buy == 800

    assert client.get("/watchlist/7/row").status_code == 200
    assert client.get("/watchlist/999/edit").status_code == 404


def test_futbin_link_can_be_removed(client: TestClient, collector: Collector) -> None:
    client.post(
        "/watchlist",
        data={"ea_id": "3", "futbin_url": "https://www.futbin.com/27/player/1/x"},
    )
    client.post("/watchlist/3", data={"futbin_url": ""})
    with collector.session_factory() as session:
        assert repo.get_source_ref(session, 3, "futbin") is None


# --- prices and player detail ----------------------------------------------


def test_prices_overview(client: TestClient, collector: Collector) -> None:
    now = datetime.now(UTC)
    add_watch(collector, 1, target_buy=1_100)
    add_watch(collector, 2)
    add_watch(collector, 3)
    add_price(collector, 1, 1_250, now - timedelta(hours=25))
    add_price(collector, 1, 1_000, now - timedelta(minutes=5))
    add_price(collector, 2, 2_000, now - timedelta(minutes=5))
    with collector.session_factory.begin() as session:
        player = repo.get_player_by_ea_id(session, 3)
        assert player is not None
        repo.set_watch_active(session, player, False)

    page = client.get("/prices").text
    assert "P1" in page
    assert "1.000" in page
    assert "-20,0 %" in page  # 1.250 -> 1.000
    assert "Kaufziel" in page  # 1.000 <= target 1.100
    assert "P2" in page
    assert "P3" not in page  # inactive


def test_prices_overview_empty(client: TestClient) -> None:
    assert "Noch keine aktiven Spieler" in client.get("/prices").text


def test_player_page_with_chart(client: TestClient, collector: Collector) -> None:
    now = datetime.now(UTC)
    add_watch(collector, 1)
    add_price(collector, 1, 1_000, now - timedelta(days=2), source="futbin")
    add_price(collector, 1, 1_100, now - timedelta(hours=1), source="futnext")
    add_price(collector, 1, 9_999, now - timedelta(days=9), source="futbin")  # outside window

    page = client.get("/players/1").text
    assert 'id="chart-data"' in page
    assert '"futbin"' in page
    assert '"futnext"' in page
    assert "1.050" in page  # average of the 7-day window
    assert "9.999" not in page


def test_player_page_unknown(client: TestClient) -> None:
    assert client.get("/players/424242").status_code == 404


def test_manual_price_entry(client: TestClient, collector: Collector) -> None:
    add_watch(collector, 1)
    response = client.post("/players/1/prices", data={"price": "1,25M"}, follow_redirects=False)
    assert response.status_code == 303
    with collector.session_factory() as session:
        snapshot = session.scalars(select(PriceSnapshot)).one()
        assert (snapshot.price, snapshot.source, snapshot.platform) == (
            1_250_000,
            "manual",
            Platform.PC,
        )

    bad = client.post("/players/1/prices", data={"price": "abc"}, follow_redirects=False)
    assert bad.headers["location"] == "/players/1?error=price"
    assert "Ungültiger Betrag" in client.get("/players/1?error=price").text


# --- status ----------------------------------------------------------------


def wait_for_run(client: TestClient, timeout: float = 5.0) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        health = client.get("/health", auth=None).json()
        if health["last_run"] and not health["collector_running"]:
            return health  # type: ignore[no-any-return]
        time.sleep(0.05)
    pytest.fail("collection did not finish")


def test_status_page_and_manual_collect(client: TestClient, collector: Collector) -> None:
    add_watch(collector, 1)
    add_watch(collector, 2)
    assert "Seit dem Start noch kein Sammellauf" in client.get("/status").text

    response = client.post("/status/collect", headers=HTMX)
    assert response.status_code == 200
    assert 'id="status-panel"' in response.text
    wait_for_run(client)

    panel = client.get("/status/panel").text
    assert "2 Spieler" in panel
    assert "2 neue Preise" in panel


def test_status_shows_and_resumes_paused_source(client: TestClient, collector: Collector) -> None:
    with collector.session_factory.begin() as session:
        repo.pause_source(session, "futbin", datetime.now(UTC) + timedelta(hours=3), "HTTP 429")
    page = client.get("/status").text
    assert "pausiert bis" in page
    assert "HTTP 429" in page

    response = client.post("/status/sources/futbin/resume", headers=HTMX)
    assert response.status_code == 200
    assert "pausiert bis" not in response.text


def test_status_collect_without_htmx_redirects(client: TestClient) -> None:
    response = client.post("/status/collect", follow_redirects=False)
    assert response.status_code == 303
    wait_for_run(client)


# --- helpers ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("1.200.000", 1_200_000),
        ("1,200,000", 1_200_000),
        ("1 200 000", 1_200_000),
        (f"1{NBSP}200{NBSP}000", 1_200_000),
        ("1.2M", 1_200_000),
        ("1,25m", 1_250_000),
        ("45k", 45_000),
        ("950", 950),
        ("", None),
        ("  ", None),
        (None, None),
    ],
)
def test_parse_coins(text: str | None, value: int | None) -> None:
    assert parse_coins(text) == value


@pytest.mark.parametrize("text", ["abc", "0", "-5", "1.2.3M", "12x"])
def test_parse_coins_rejects(text: str) -> None:
    with pytest.raises(ValueError, match="amount"):
        parse_coins(text)


def test_formatters() -> None:
    assert format_coins(1_250_000) == "1.250.000"
    assert format_coins(None) == chr(0x2013)
    assert format_pct(-20.0) == "-20,0 %"
    assert format_pct(3.25) == "+3,2 %"


# --- analysis & signals (Phase 4) ------------------------------------------


def seed_dip_and_uev(collector: Collector) -> None:
    """Card 1: clear dip below the 7-day mean. Card 2: thin supply, rising, ÜV chance."""
    now = datetime.now(UTC)
    add_watch(collector, 1)
    add_watch(collector, 2)
    for hours in range(160, 0, -1):
        add_price(collector, 1, 10_000, now - timedelta(hours=hours + 1), source="futbin")
        rising = round(85_000 + 15_000 * (160 - hours) / 160)
        add_price(collector, 2, rising, now - timedelta(hours=hours + 1), source="futbin")
    add_price(collector, 1, 8_000, now - timedelta(minutes=5), source="futbin")
    add_price(collector, 2, 100_000, now - timedelta(minutes=5), source="futbin")
    with collector.session_factory.begin() as session:
        for ea_id, listings in ((1, (8_000, 8_100, 8_200, 8_300, 8_400)), (2, (100_000, 130_000))):
            player = repo.get_player_by_ea_id(session, ea_id)
            assert player is not None
            repo.record_market_state(
                session, player, Platform.PC, "futbin", now, listings, 150, 300_000
            )


def test_signals_page(client: TestClient, collector: Collector) -> None:
    seed_dip_and_uev(collector)
    page = client.get("/signals").text
    assert "Kauf-Dip" in page
    assert "ÜV-Chance" in page
    assert "129.000" in page  # listing just below the next offer

    only_dip = client.get("/signals?rule=BUY_DIP").text
    assert "Kauf-Dip" in only_dip
    assert "129.000" not in only_dip
    assert "Aktuell keine Signale" in client.get("/signals?rule=SELL_TARGET").text


def test_prices_overview_shows_signal_and_extinct_badges(
    client: TestClient, collector: Collector
) -> None:
    seed_dip_and_uev(collector)
    add_watch(collector, 3)
    add_price(collector, 3, 50_000, datetime.now(UTC) - timedelta(hours=2))
    with collector.session_factory.begin() as session:
        player = repo.get_player_by_ea_id(session, 3)
        assert player is not None
        repo.record_market_state(
            session, player, Platform.PC, "futbin", datetime.now(UTC), (), 150, 200_000
        )
    page = client.get("/prices").text
    assert "/signals?rule=BUY_DIP" in page
    assert "/signals?rule=OVERPRICE_CHANCE" in page
    assert "extinct" in page


def test_player_page_shows_analysis(client: TestClient, collector: Collector) -> None:
    seed_dip_and_uev(collector)
    page = client.get("/players/2").text
    assert "Angebotslage" in page
    assert "130.000" in page
    assert "ÜV-Score" in page
    assert "Einstellen zu" in page
    assert 'id="profile-data"' in page
    dip = client.get("/players/1").text
    assert "Kaufen bis max." in dip


def test_player_page_marks_outlier(client: TestClient, collector: Collector) -> None:
    add_watch(collector, 5)
    add_price(collector, 5, 8_150_000, datetime.now(UTC) - timedelta(minutes=5), source="futbin")
    with collector.session_factory.begin() as session:
        player = repo.get_player_by_ea_id(session, 5)
        assert player is not None
        repo.record_market_state(
            session,
            player,
            Platform.PC,
            "futbin",
            datetime.now(UTC),
            (5_500_000, 8_150_000),
            75_000,
            14_500_000,
        )
    page = client.get("/players/5").text
    assert "gilt als Ausreißer" in page
    assert "Marktpreis 8.150.000" in page


def test_player_page_without_supply_data(client: TestClient, collector: Collector) -> None:
    add_watch(collector, 6)
    add_price(collector, 6, 1_000, datetime.now(UTC))
    assert "Keine Angebotsdaten" in client.get("/players/6").text


def test_outlier_does_not_inflate_overprice_score(client: TestClient, collector: Collector) -> None:
    from fcast.analysis.service import analyze_player

    now = datetime.now(UTC)
    add_watch(collector, 5)
    for hours in range(48, 0, -1):
        add_price(collector, 5, 8_150_000, now - timedelta(hours=hours), source="futbin")
    with collector.session_factory.begin() as session:
        player = repo.get_player_by_ea_id(session, 5)
        assert player is not None
        repo.record_market_state(
            session,
            player,
            Platform.PC,
            "futbin",
            now,
            (5_500_000, 8_150_000, 8_300_000, 8_350_000, 8_400_000),
            75_000,
            14_500_000,
        )
        result = analyze_player(session, player, collector.settings, now)
    assert result.outlier == 5_500_000
    assert result.supply is not None
    assert result.supply.listings[0] == 8_150_000
    assert result.overprice is not None
    assert result.overprice.score < 40  # the 48 % gap to the outlier must not count


# --- alerts page (Phase 5) -------------------------------------------------


def test_alerts_page_shows_channel_and_defaults(client: TestClient) -> None:
    page = client.get("/alerts").text
    assert "Kanal: <strong>log</strong>" in page
    assert 'name="quiet_start" type="time" value="00:00"' in page
    assert "Noch keine Alerts gesendet." in page


def test_alerts_settings_are_saved(client: TestClient, collector: Collector) -> None:
    from fcast.alerts.config import load_alert_config

    response = client.post(
        "/alerts/settings",
        data={
            "enabled": "on",
            "buy_dip": "on",
            "system": "on",
            "cooldown_h": "2,5",
            "min_profit": "10k",
            "quiet_start": "23:00",
            "quiet_end": "06:30",
        },
    )
    assert response.status_code == 200
    assert "Gespeichert." in response.text
    with collector.session_factory() as session:
        config = load_alert_config(session, collector.settings)
    assert config.enabled is True
    assert config.cooldown_h == 2.5
    assert config.min_profit == 10_000
    assert (config.quiet_start, config.quiet_end) == ("23:00", "06:30")
    assert config.buy_dip is True
    assert config.overprice is False
    assert config.sell_target is False


def test_alerts_settings_validation(client: TestClient) -> None:
    response = client.post(
        "/alerts/settings",
        data={"cooldown_h": "-1", "quiet_start": "25:00", "quiet_end": "07:00", "min_profit": "x"},
    )
    assert response.status_code == 422
    assert "Cooldown" in response.text
    assert "Ruhezeit Beginn" in response.text
    assert "Mindestprofit" in response.text


def test_alerts_test_push_and_history(client: TestClient, collector: Collector) -> None:
    from fcast.alerts.notifier import Notification, Notifier

    sent: list[Notification] = []

    class Recorder(Notifier):
        name = "recorder"

        async def send(self, notification: Notification) -> None:
            sent.append(notification)

    collector.alerts.notifier = Recorder()
    response = client.post("/alerts/test")
    assert "Test über recorder gesendet." in response.text
    assert sent[0].title == "FCast Test"

    with collector.session_factory.begin() as session:
        repo.log_alert(session, "BUY_DIP", "Kauf-Dip: P1\nPreis 1.000")
    page = client.get("/alerts").text
    assert "Kauf-Dip: P1" in page
    assert "Preis 1.000" in page


@pytest.mark.parametrize(
    ("text", "ea_id"),
    [
        ("231747", 231747),
        (" 231747 ", 231747),
        ("https://www.fut.gg/players/231747-kylian-mbappe/27-231747/", 231747),
        ("https://www.fut.gg/players/231747-kylian-mbappe/27-50563395/", 50563395),
        ("fut.gg/players/231747-kylian-mbappe/27-50563395", 50563395),
        ("https://www.futnext.com/players/mbappe/231747", 231747),
        ("https://www.futnext.com/de/players/mbappe/50563395?x=1", 50563395),
    ],
)
def test_parse_ea_id_from_number_or_link(text: str, ea_id: int) -> None:
    from fcast.web.forms import parse_ea_id

    assert parse_ea_id(text) == ea_id


@pytest.mark.parametrize(
    ("text", "hint"),
    [
        ("https://www.futbin.com/27/player/21487/maradona", "FUTBIN-Link enthält keine EA-ID"),
        ("abc", "FUT.GG"),
        ("0", "FUT.GG"),
        ("", "FUT.GG"),
    ],
)
def test_parse_ea_id_errors(text: str, hint: str) -> None:
    from fcast.web.forms import parse_ea_id

    with pytest.raises(ValueError, match=hint):
        parse_ea_id(text)


def test_watchlist_add_with_futgg_link(client: TestClient, collector: Collector) -> None:
    response = client.post(
        "/watchlist",
        data={
            "ea_id": "https://www.fut.gg/players/231747-kylian-mbappe/27-50563395/",
            "futbin_url": "https://www.futbin.com/27/player/99999/mbappe",
        },
        headers=HTMX,
    )
    assert "gespeichert" in response.text
    with collector.session_factory() as session:
        assert repo.get_source_ref(session, 50563395, "futbin") == "/27/player/99999/mbappe"


# --- FUTBIN link only (EA id from the card image) --------------------------


def test_watchlist_add_with_only_futbin_link_for_special_card(
    client: TestClient, collector: Collector
) -> None:
    response = client.post(
        "/watchlist",
        data={"ea_id": "", "futbin_url": "https://www.futbin.com/27/player/22947/michael-olise"},
        headers=HTMX,
    )
    assert "Michael Olise (91) gespeichert" in response.text
    with collector.session_factory() as session:
        player = repo.get_player_by_ea_id(session, 50579475)  # TOTW card id, not the player id
        assert player is not None
        assert (player.chem_style, player.games_used, player.goals_per_game) == (
            "Hunter",
            535,
            0.802,
        )
        assert player.card_type == "Team of the Week"
        assert repo.get_source_ref(session, 50579475, "futbin") == "/27/player/22947/michael-olise"
    page = client.get("/players/50579475").text
    assert "Hunter <b>77%</b>" in page
    assert "Artist <b>8%</b>" in page
    assert "Engine <b>8%</b>" in page
    assert "Hunter <b>77%</b>" in client.get("/watchlist").text
    assert "535" in page


def test_watchlist_add_rejects_mismatching_links(client: TestClient) -> None:
    response = client.post(
        "/watchlist",
        data={
            "ea_id": "https://www.fut.gg/players/231747-kylian-mbappe/27-231747/",
            "futbin_url": "https://www.futbin.com/27/player/22947/michael-olise",
        },
        headers=HTMX,
    )
    assert "anderen Karte (EA-ID 50579475)" in response.text
    assert "hx-swap-oob" not in response.text


@pytest.mark.parametrize(
    ("data", "error"),
    [
        ({"futbin_url": "https://www.futbin.com/27/player/1/unknown"}, "nicht gefunden"),
        ({"ea_id": "", "futbin_url": ""}, "Bitte EA-ID, FUT.GG-Link oder FUTBIN-Link"),
    ],
)
def test_watchlist_add_futbin_errors(client: TestClient, data: dict[str, str], error: str) -> None:
    assert error in client.post("/watchlist", data=data, headers=HTMX).text


def test_paused_futbin_is_not_asked(client: TestClient, collector: Collector) -> None:
    with collector.session_factory.begin() as session:
        repo.pause_source(session, "futbin", datetime.now(UTC) + timedelta(hours=5), "HTTP 429")
    response = client.post(
        "/watchlist",
        data={"futbin_url": "https://www.futbin.com/27/player/21487/maradona"},
        headers=HTMX,
    )
    assert "FUTBIN ist gerade pausiert" in response.text
    assert FUTBIN_REQUESTS == []


def test_edit_row_checks_new_futbin_link(client: TestClient, collector: Collector) -> None:
    add_watch(collector, 190042)
    bad = client.post(
        "/watchlist/190042",
        data={"futbin_url": "https://www.futbin.com/27/player/22947/michael-olise"},
    )
    assert "anderen Karte" in bad.text
    good = client.post(
        "/watchlist/190042",
        data={"futbin_url": "https://www.futbin.com/27/player/21487/maradona"},
    )
    assert "Diego Maradona" in good.text
    with collector.session_factory() as session:
        player = repo.get_player_by_ea_id(session, 190042)
        assert player is not None
        assert player.chem_style == "Hunter"  # most popular with the community
        assert player.chem_styles == [("Hunter", 60), ("Hawk", 40), ("Basic", 0)]


def test_futbin_link_is_found_in_background(client: TestClient, collector: Collector) -> None:
    response = client.post(
        "/watchlist",
        data={"ea_id": "https://www.fut.gg/players/247827-michael-olise/27-50579475/"},
        headers=HTMX,
    )
    assert "wird im Hintergrund gesucht" in response.text
    deadline = time.monotonic() + 5
    ref = None
    while time.monotonic() < deadline and ref is None:
        time.sleep(0.05)
        client.get("/health", auth=None)  # lets the app's event loop run the background task
        with collector.session_factory() as session:
            ref = repo.get_source_ref(session, 50579475, "futbin")
    assert ref == "/27/player/22947/michael-olise"
    with collector.session_factory() as session:
        player = repo.get_player_by_ea_id(session, 50579475)
        assert player is not None
        assert player.chem_style == "Hunter"


def test_overview_shows_chem_chips(client: TestClient, collector: Collector) -> None:
    add_watch(collector, 1)
    add_watch(collector, 2)
    with collector.session_factory.begin() as session:
        repo.upsert_player(
            session, 1, repo.PlayerDetails(chem_styles_raw="Engine:48|Hunter:24|Deadeye:10")
        )
        repo.upsert_player(session, 2, repo.PlayerDetails(chem_style="GK Basic"))
    page = client.get("/prices").text
    assert 'class="chem chem-midfield"' in page  # Engine
    assert 'class="chem chem-pace"' in page  # Hunter
    assert 'class="chem chem-attack"' in page  # Deadeye
    assert "Engine <b>48%</b>" in page
    assert 'class="chem chem-keeper"' in page  # fallback without percentages
    assert "GK Basic" in page


def test_chem_groups() -> None:
    from fcast.web.chem import chem_group

    assert chem_group("Hunter") == "pace"
    assert chem_group("GK Basic") == "keeper"
    assert chem_group("Unbekannt") == "other"


# --- TOTW page ----------------------------------------------------------------------


def test_totw_page_shows_candidates_and_review(client: TestClient, collector: Collector) -> None:
    from fcast.db.models import TotwActual, TotwPrediction

    now = datetime.now(UTC)
    week = collector.totw.upcoming(now)
    released = collector.totw.last_released(now)
    with collector.session_factory.begin() as session:
        session.add(
            TotwPrediction(
                week=week.number,
                key="bl1:1",
                rank=1,
                name="M. Olise",
                team="FC Bayern München",
                league="bl1",
                score=15.0,
                goals=3,
                reasons="3 Tore · Hattrick · Sieg",
                ea_id=247827,
                futbin_ref="/27/player/62/michael-olise",
                card_name="Michael Olise (90)",
                price=245_000,
                chem_styles_raw="Hunter:77",
            )
        )
        if released is not None:
            session.add(
                TotwPrediction(
                    week=released.number,
                    key="bl1:1",
                    rank=1,
                    name="M. Olise",
                    team="FCB",
                    league="bl1",
                    score=1,
                    goals=1,
                    reasons="",
                    ea_id=None,
                    futbin_ref=None,
                    card_name=None,
                    price=None,
                    chem_styles_raw=None,
                )
            )
            session.add(
                TotwActual(
                    week=released.number, futbin_id=1, slug="michael-olise", name="Michael Olise"
                )
            )
    page = client.get("/totw").text
    assert f"TOTW {week.number}" in page
    assert "M. Olise" in page
    assert "3 Tore · Hattrick · Sieg" in page
    assert "245.000" in page
    assert "Hunter <b>77%</b>" in page
    assert 'name="futbin_url" value="https://www.futbin.com/27/player/62/michael-olise"' in page
    if released is not None:
        assert "1 von 1" in page


def test_totw_refresh_runs_in_background(client: TestClient, collector: Collector) -> None:
    calls: list[int] = []

    async def fake_refresh() -> None:
        calls.append(1)

    collector.refresh_totw = fake_refresh  # type: ignore[method-assign]
    response = client.post("/totw/refresh", follow_redirects=False)
    assert response.status_code == 303
    client.get("/health", auth=None)  # let the event loop run the task
    assert calls == [1]
    assert "Noch keine Kandidaten" in client.get("/totw").text


def test_weekday_names_are_german(client: TestClient) -> None:
    from fcast.web.app import build_templates

    templates = build_templates(make_settings(Path(".")))
    format_dt = templates.env.filters["dt"]
    wednesday = datetime(2026, 9, 30, 17, 0, tzinfo=UTC)
    assert format_dt(wednesday, "%a, %d.%m. %H:%M") == "Mi, 30.09. 19:00"
