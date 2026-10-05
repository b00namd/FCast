"""Alert engine, configuration and ntfy client (mocked notifier, no real pushes)."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy.orm import Session, sessionmaker

from fcast.alerts.config import AlertConfig, load_alert_config, save_alert_config
from fcast.alerts.engine import (
    COLLECT_FAILED,
    AlertEngine,
    Outcome,
    build_notifier,
    signal_notification,
)
from fcast.alerts.notifier import (
    LogNotifier,
    Notification,
    Notifier,
    NotifierError,
    NtfyNotifier,
    Priority,
)
from fcast.analysis import signals as sig
from fcast.collector.job import CollectResult
from fcast.collector.service import Collector, failed_players
from fcast.config import Platform, Settings
from fcast.db import repositories as repo
from fcast.db.base import Base
from fcast.db.session import create_db_engine, create_session_factory

BERLIN = ZoneInfo("Europe/Berlin")
# 10:00 UTC == 12:00 Berlin (summer time), outside the default quiet hours.
NOON = datetime(2026, 9, 30, 10, 0, tzinfo=UTC)


class FakeNotifier(Notifier):
    name = "fake"

    def __init__(self, fail: bool = False) -> None:
        self.sent: list[Notification] = []
        self.fail = fail

    async def send(self, notification: Notification) -> None:
        if self.fail:
            raise NotifierError("offline")
        self.sent.append(notification)


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {"platform": "pc", "sources": "", "dashboard_url": "http://fcast"}
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


@pytest.fixture
def factory() -> sessionmaker[Session]:
    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def seed_dip(factory: sessionmaker[Session], ea_id: int = 1, now: datetime = NOON) -> None:
    """A clear BUY_DIP: 7 days at 10.000, now 8.000."""
    with factory.begin() as session:
        player = repo.upsert_player(session, ea_id, repo.PlayerDetails(name=f"Card {ea_id}"))
        repo.set_watch(session, player)
        repo.set_source_ref(session, player, "futbin", f"/27/player/{ea_id}/card")
        for hours in range(160, 0, -1):
            repo.add_snapshot(
                session, player, Platform.PC, 10_000, "futbin", now - timedelta(hours=hours)
            )
        repo.add_snapshot(session, player, Platform.PC, 8_000, "futbin", now)


def alerts_logged(factory: sessionmaker[Session]) -> list[str]:
    with factory() as session:
        return [a.rule for a in repo.list_alerts(session)]


# --- configuration ---------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "end", "local", "quiet"),
    [
        ("00:00", "07:00", "03:00", True),
        ("00:00", "07:00", "07:00", False),
        ("00:00", "07:00", "12:00", False),
        ("23:00", "07:00", "23:30", True),  # spans midnight
        ("23:00", "07:00", "06:59", True),
        ("23:00", "07:00", "22:59", False),
        ("08:00", "08:00", "08:00", False),  # same time disables quiet hours
    ],
)
def test_quiet_hours(start: str, end: str, local: str, quiet: bool) -> None:
    hour, minute = map(int, local.split(":"))
    now = datetime(2026, 9, 30, hour, minute, tzinfo=BERLIN).astimezone(UTC)
    assert AlertConfig(quiet_start=start, quiet_end=end).is_quiet(now, BERLIN) is quiet


def test_config_defaults_and_overrides(factory: sessionmaker[Session]) -> None:
    settings = make_settings(alert_cooldown_h=3, quiet_hours_start="22:00", alert_min_profit=100)
    with factory.begin() as session:
        config = load_alert_config(session, settings)
        assert (config.cooldown_h, config.quiet_start, config.min_profit) == (3, "22:00", 100)

        save_alert_config(session, AlertConfig(enabled=False, cooldown_h=1.5, overprice=False))
        stored = load_alert_config(session, settings)
        assert stored.enabled is False
        assert stored.cooldown_h == 1.5
        assert stored.overprice is False
        assert stored.buy_dip is True

        repo.set_app_setting(session, "alerts.cooldown_h", "kaputt")
        assert load_alert_config(session, settings).cooldown_h == 3  # broken value ignored


# --- notifications ---------------------------------------------------------


def test_signal_notification_content() -> None:
    signal = sig.Signal(
        rule=sig.Rule.BUY_DIP,
        ea_id=42,
        name="Mbappé (91)",
        price=4_100_000,
        reference=4_600_000,
        recommended=4_150_000,
        expected_profit=270_000,
        score=None,
        reasons=("-10,9 % gegenüber Ø 7 Tage",),
    )
    note = signal_notification(signal, "http://fcast/", "/27/player/1/mbappe")
    assert note.title == "Kauf-Dip: Mbappé (91)"
    assert "Preis 4.100.000 (Ø 7 Tage 4.600.000)" in note.message
    assert "Kaufen bis max. 4.150.000" in note.message
    assert "Erwarteter Profit nach Steuer: 270.000" in note.message
    assert note.priority is Priority.HIGH
    assert note.click_url == "http://fcast/players/42"
    assert note.actions == (
        ("Dashboard", "http://fcast/players/42"),
        ("FUTBIN", "https://www.futbin.com/27/player/1/mbappe"),
    )
    plain = signal_notification(signal, None, None)
    assert plain.click_url is None
    assert plain.actions == ()


def test_build_notifier() -> None:
    assert isinstance(build_notifier(make_settings()), LogNotifier)
    ntfy = build_notifier(make_settings(ntfy_url="http://ntfy.local", ntfy_token="tk_x"))
    assert isinstance(ntfy, NtfyNotifier)


async def test_ntfy_payload_and_auth() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "abc"})

    notifier = NtfyNotifier(
        "http://ntfy.local/", "fcast", "tk_secret", transport=httpx.MockTransport(handler)
    )
    await notifier.send(
        Notification(
            title="Kauf-Dip: Mbappé",
            message="Zeile 1\nZeile 2",
            priority=Priority.HIGH,
            tags=("moneybag",),
            click_url="http://fcast/players/1",
            actions=(("Dashboard", "http://fcast/players/1"),),
        )
    )
    await notifier.aclose()

    (request,) = requests
    assert str(request.url) == "http://ntfy.local"
    assert request.headers["Authorization"] == "Bearer tk_secret"
    body = json.loads(request.content)
    assert body["topic"] == "fcast"
    assert body["title"] == "Kauf-Dip: Mbappé"  # UTF-8 survives thanks to the JSON API
    assert body["priority"] == 4
    assert body["tags"] == ["moneybag"]
    assert body["click"] == "http://fcast/players/1"
    assert body["actions"][0]["action"] == "view"


@pytest.mark.parametrize("failure", ["status", "network"])
async def test_ntfy_errors(failure: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if failure == "network":
            raise httpx.ConnectError("down", request=request)
        return httpx.Response(403, text="forbidden")

    notifier = NtfyNotifier("http://ntfy.local", "fcast", transport=httpx.MockTransport(handler))
    with pytest.raises(NotifierError):
        await notifier.send(Notification(title="x", message="y"))
    await notifier.aclose()


# --- engine ----------------------------------------------------------------


async def test_signal_alert_is_sent_once_within_cooldown(factory: sessionmaker[Session]) -> None:
    seed_dip(factory)
    notifier = FakeNotifier()
    engine = AlertEngine(notifier, make_settings())

    report = await engine.process(factory, NOON)
    assert report.count(Outcome.SENT) == 1
    assert notifier.sent[0].title == "Kauf-Dip: Card 1"
    assert alerts_logged(factory) == ["BUY_DIP"]

    again = await engine.process(factory, NOON + timedelta(hours=5))
    assert again.count(Outcome.COOLDOWN) == 1
    assert len(notifier.sent) == 1

    later = await engine.process(factory, NOON + timedelta(hours=6, minutes=1))
    assert later.count(Outcome.SENT) == 1
    assert len(notifier.sent) == 2


async def test_cooldown_is_per_card(factory: sessionmaker[Session]) -> None:
    seed_dip(factory, 1)
    seed_dip(factory, 2)
    notifier = FakeNotifier()
    await AlertEngine(notifier, make_settings()).process(factory, NOON)
    assert sorted(n.title for n in notifier.sent) == ["Kauf-Dip: Card 1", "Kauf-Dip: Card 2"]


async def test_quiet_hours_hold_back_and_send_later(factory: sessionmaker[Session]) -> None:
    night = datetime(2026, 9, 30, 3, 0, tzinfo=BERLIN).astimezone(UTC)
    seed_dip(factory, now=night)
    notifier = FakeNotifier()
    engine = AlertEngine(notifier, make_settings())

    report = await engine.process(factory, night)
    assert report.count(Outcome.QUIET) == 1
    assert notifier.sent == []
    assert alerts_logged(factory) == []  # not logged, so it is sent after the quiet hours

    morning = datetime(2026, 9, 30, 7, 30, tzinfo=BERLIN).astimezone(UTC)
    assert (await engine.process(factory, morning)).count(Outcome.SENT) == 1


async def test_disabled_rule_and_min_profit(factory: sessionmaker[Session]) -> None:
    seed_dip(factory)
    notifier = FakeNotifier()
    engine = AlertEngine(notifier, make_settings())
    with factory.begin() as session:
        save_alert_config(session, AlertConfig(buy_dip=False))
    assert (await engine.process(factory, NOON)).count(Outcome.DISABLED) == 1

    with factory.begin() as session:
        save_alert_config(session, AlertConfig(min_profit=5_000))  # the dip earns ~1.400
    assert (await engine.process(factory, NOON)).count(Outcome.FILTERED) == 1

    with factory.begin() as session:
        save_alert_config(session, AlertConfig(enabled=False))
    assert (await engine.process(factory, NOON)).count(Outcome.DISABLED) == 1
    assert notifier.sent == []


async def test_failed_delivery_is_retried_next_run(factory: sessionmaker[Session]) -> None:
    seed_dip(factory)
    broken = FakeNotifier(fail=True)
    report = await AlertEngine(broken, make_settings()).process(factory, NOON)
    assert report.count(Outcome.FAILED) == 1
    assert alerts_logged(factory) == []

    working = FakeNotifier()
    await AlertEngine(working, make_settings()).process(factory, NOON + timedelta(minutes=30))
    assert len(working.sent) == 1


async def test_system_alerts(factory: sessionmaker[Session]) -> None:
    notifier = FakeNotifier()
    engine = AlertEngine(notifier, make_settings(source_pause_h=24))
    report = await engine.process(factory, NOON, paused={"futbin": "HTTP 429"}, failed_players=3)
    assert report.count(Outcome.SENT) == 2
    titles = sorted(n.title for n in notifier.sent)
    assert titles == ["Quelle pausiert: futbin", "Sammellauf ohne Preise"]
    assert all(n.click_url == "http://fcast/status" for n in notifier.sent)
    assert sorted(alerts_logged(factory)) == [COLLECT_FAILED, "SOURCE_PAUSED:futbin"]

    # A pause of another source is a different alert; the same one is in cooldown.
    report = await engine.process(
        factory, NOON + timedelta(hours=1), paused={"futbin": "HTTP 429", "futnext": "HTTP 403"}
    )
    assert report.count(Outcome.SENT) == 1
    assert report.count(Outcome.COOLDOWN) == 1


def test_failed_players() -> None:
    now = NOON
    ok = CollectResult(started_at=now, players=3, stored=3)
    assert failed_players(ok) is None
    broken = CollectResult(started_at=now, players=3, errors={"futbin": ["x"]})
    assert failed_players(broken) == 3
    extinct_only = CollectResult(started_at=now, players=1, extinct=[1], errors={"x": ["y"]})
    assert failed_players(extinct_only) is None
    empty = CollectResult(started_at=now, players=0)
    assert failed_players(empty) is None


async def test_collector_sends_alerts_after_run(tmp_path: Path) -> None:
    settings = make_settings(db_path=tmp_path / "fcast.db")
    notifier = FakeNotifier()
    collector = Collector(settings, sources=[], alerts=AlertEngine(notifier, settings))
    now = datetime.now(UTC)
    seed_dip(collector.session_factory, now=now)
    with collector.session_factory.begin() as session:
        save_alert_config(session, AlertConfig(quiet_start="00:00", quiet_end="00:00"))
    try:
        await collector.run_once()
    finally:
        await collector.aclose()
    assert [n.title for n in notifier.sent] == ["Kauf-Dip: Card 1"]


# --- radar --------------------------------------------------------------------------------


def seed_radar(factory: sessionmaker[Session], now: datetime = NOON) -> None:
    """A radar card (not watched) rising steadily over the last 12 hours, plus fodder +20 %."""
    from fcast.db.models import FodderPrice, RadarCard

    with factory.begin() as session:
        player = repo.upsert_player(session, 77, repo.PlayerDetails(name="Radar Card"))
        session.add(
            RadarCard(
                futbin_ref="/27/player/77/radar-card",
                list_name="latest",
                player_id=player.id,
                first_seen_at=now,
                last_seen_at=now,
            )
        )
        prices = [20_000, 20_000, 20_250, 20_500, 20_500, 20_750, 21_000, 21_250, 21_500]
        for i, price in enumerate(prices):
            at = now - timedelta(hours=8 - i)
            repo.add_snapshot(session, player, Platform.PC, price, "futbin", at)
        for hours, price in ((24, 3_500), (0, 4_200)):
            session.add(
                FodderPrice(
                    platform=Platform.PC,
                    rating=86,
                    price=price,
                    observed_at=now - timedelta(hours=hours),
                )
            )


async def test_radar_alerts_once_a_day(factory: sessionmaker[Session]) -> None:
    seed_radar(factory)
    notifier = FakeNotifier()
    engine = AlertEngine(notifier, make_settings())

    report = await engine.process(factory, NOON)
    assert report.count(Outcome.SENT) == 2
    titles = sorted(n.title for n in notifier.sent)
    assert titles == ["Futter zieht an: 86er", "Radar: Radar Card"]
    radar_push = next(n for n in notifier.sent if n.title.startswith("Radar"))
    assert "Trend-Start" in radar_push.message
    assert "Noch nicht auf der Watchlist" in radar_push.message
    assert radar_push.click_url == "http://fcast/radar"
    assert sorted(alerts_logged(factory)) == ["RADAR", "RADAR:FODDER86"]

    # Radar hits wait 24 h, not the usual 6 h.
    with factory.begin() as session:
        for alert in repo.list_alerts(session):
            alert.sent_at = NOON - timedelta(hours=7)
    again = await engine.process(factory, NOON)
    assert again.count(Outcome.COOLDOWN) == 2


async def test_radar_alerts_can_be_switched_off(factory: sessionmaker[Session]) -> None:
    seed_radar(factory)
    with factory.begin() as session:
        save_alert_config(session, AlertConfig(radar=False))
    report = await AlertEngine(FakeNotifier(), make_settings()).process(factory, NOON)
    assert report.count(Outcome.DISABLED) == 2


async def test_buy_signals_above_the_coin_balance_are_not_pushed(
    factory: sessionmaker[Session],
) -> None:
    from fcast.portfolio import coins as wallet

    seed_dip(factory)  # buy at 8.000
    with factory.begin() as session:
        wallet.set_balance(session, 5_000, now=NOON - timedelta(hours=1))
    notifier = FakeNotifier()
    report = await AlertEngine(notifier, make_settings()).process(factory, NOON)
    assert report.count(Outcome.TOO_EXPENSIVE) == 1
    assert notifier.sent == []

    with factory.begin() as session:
        wallet.set_balance(session, 8_000, now=NOON)
    report = await AlertEngine(notifier, make_settings()).process(factory, NOON)
    assert report.count(Outcome.SENT) == 1
