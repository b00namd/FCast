from pathlib import Path

import pytest
from typer.testing import CliRunner

from fcast import __version__
from fcast.cli import app, format_coins
from fcast.config import get_settings

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == f"fcast {__version__}"


def test_config_masks_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FCAST_NTFY_TOKEN", "super-secret")
    get_settings.cache_clear()
    try:
        result = runner.invoke(app, ["config"])
    finally:
        get_settings.cache_clear()
    assert result.exit_code == 0
    assert "super-secret" not in result.output
    assert "platform" in result.output


@pytest.mark.parametrize(
    ("value", "expected"), [(None, "-"), (950, "950"), (12_500, "12.500"), (1_250_000, "1.250.000")]
)
def test_format_coins(value: int | None, expected: str) -> None:
    assert format_coins(value) == expected


def test_db_upgrade_creates_database(db_path: Path) -> None:
    result = runner.invoke(app, ["db", "upgrade"])
    assert result.exit_code == 0, result.output
    assert db_path.exists()


def test_watch_add_and_list(db_path: Path) -> None:
    result = runner.invoke(
        app, ["watch", "add", "231747", "--buy", "10000", "--sell", "12500", "--name", "Mbappé"]
    )
    assert result.exit_code == 0, result.output
    assert "Watching Mbappé" in result.output

    runner.invoke(app, ["watch", "add", "158023", "--buy", "5000"])

    result = runner.invoke(app, ["watch", "list"])
    assert result.exit_code == 0, result.output
    assert "231747" in result.output
    assert "12.500" in result.output
    assert "#158023" in result.output


def test_watch_add_updates_existing_entry(db_path: Path) -> None:
    runner.invoke(app, ["watch", "add", "1", "--buy", "1000", "--name", "Alpha"])
    result = runner.invoke(app, ["watch", "add", "1", "--buy", "900"])
    assert result.exit_code == 0, result.output
    # The name from the first call is kept.
    assert "Watching Alpha" in result.output

    result = runner.invoke(app, ["watch", "list"])
    assert "900" in result.output
    assert "1.000" not in result.output


def test_watch_add_warns_when_sell_below_tax_break_even(db_path: Path) -> None:
    result = runner.invoke(app, ["watch", "add", "1", "--buy", "10000", "--sell", "10200"])
    assert result.exit_code == 0
    assert "Warning" in result.output


def test_watch_add_rejects_invalid_price(db_path: Path) -> None:
    result = runner.invoke(app, ["watch", "add", "1", "--buy", "0"])
    assert result.exit_code != 0


def test_watch_list_empty(db_path: Path) -> None:
    result = runner.invoke(app, ["watch", "list"])
    assert result.exit_code == 0
    assert "Watchlist is empty." in result.output


FIXTURE_CSV = Path(__file__).parent / "fixtures" / "manual" / "prices.csv"


def test_collect_once_with_manual_csv(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FCAST_MANUAL_CSV", str(FIXTURE_CSV))
    get_settings.cache_clear()
    runner.invoke(app, ["watch", "add", "231747", "--buy", "1100000"])
    runner.invoke(app, ["watch", "add", "999999"])

    result = runner.invoke(app, ["collect", "--once"])
    assert result.exit_code == 0, result.output
    assert "2 players: 1 new snapshots, 0 unchanged, 0 extinct, 1 without price" in result.output

    result = runner.invoke(app, ["collect", "--once"])
    assert "0 new snapshots, 1 unchanged" in result.output

    # Player details from the CSV are now known.
    listing = runner.invoke(app, ["watch", "list"], env={"COLUMNS": "200"}).output
    assert "Kylian Mbappé (91)" in listing


def test_collect_once_without_sources(db_path: Path) -> None:
    result = runner.invoke(app, ["collect", "--once"])
    assert result.exit_code == 0, result.output
    assert "0 players" in result.output


def test_prices_import(db_path: Path) -> None:
    result = runner.invoke(app, ["prices", "import", str(FIXTURE_CSV)])
    assert result.exit_code == 0, result.output
    assert "Imported 4 snapshots (0 already present)." in result.output

    result = runner.invoke(app, ["prices", "import", str(FIXTURE_CSV)])
    assert "Imported 0 snapshots (4 already present)." in result.output


def test_prices_import_reports_format_errors(db_path: Path, tmp_path: Path) -> None:
    bad = tmp_path / "bad.csv"
    bad.write_text("ea_id,price\n1,abc\n", encoding="utf-8")
    result = runner.invoke(app, ["prices", "import", str(bad)])
    assert result.exit_code == 1
    assert "invalid price" in result.output


def test_watch_add_with_futbin_link(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fcast.sources.base import PlayerInfo
    from fcast.sources.futbin import FutbinSource

    async def fake_resolve(self: FutbinSource, ref: str) -> PlayerInfo:
        return PlayerInfo(ea_id=190042, name="Diego Maradona", rating=95)

    monkeypatch.setattr(FutbinSource, "resolve_card", fake_resolve)
    result = runner.invoke(
        app,
        ["watch", "add", "190042", "--futbin", "https://www.futbin.com/27/player/21487/maradona"],
    )
    assert result.exit_code == 0, result.output
    assert "futbin" in runner.invoke(app, ["watch", "list"]).output


def test_watch_add_rejects_invalid_futbin_link(db_path: Path) -> None:
    result = runner.invoke(app, ["watch", "add", "1", "--futbin", "https://www.fut.gg/x"])
    assert result.exit_code != 0
    assert "FUTBIN" in result.output


def test_sources_status_and_resume(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import UTC, datetime, timedelta

    from fcast.db import repositories as repo
    from fcast.db.session import create_db_engine, create_session_factory

    monkeypatch.setenv("FCAST_SOURCES", "futbin,futnext")
    get_settings.cache_clear()
    assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
    engine = create_db_engine(get_settings().db_url)
    with create_session_factory(engine).begin() as session:
        repo.pause_source(session, "futbin", datetime.now(UTC) + timedelta(hours=5), "HTTP 429")
    engine.dispose()

    result = runner.invoke(app, ["sources", "status"])
    assert result.exit_code == 0, result.output
    assert "futbin" in result.output
    assert "futnext" in result.output
    assert "HTTP 429" in result.output

    result = runner.invoke(app, ["sources", "resume", "futbin"])
    assert result.exit_code == 0
    assert "HTTP 429" not in runner.invoke(app, ["sources", "status"]).output


def test_analyze_and_signals_commands(db_path: Path) -> None:
    from datetime import UTC, datetime, timedelta

    from fcast.config import Platform
    from fcast.db import repositories as repo
    from fcast.db.session import create_db_engine, create_session_factory

    assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
    engine = create_db_engine(get_settings().db_url)
    now = datetime.now(UTC)
    with create_session_factory(engine).begin() as session:
        player = repo.upsert_player(session, 1, repo.PlayerDetails(name="Dip"))
        repo.set_watch(session, player)
        for hours in range(100, 0, -1):
            repo.add_snapshot(
                session, player, Platform.CONSOLE, 10_000, "futbin", now - timedelta(hours=hours)
            )
        repo.add_snapshot(session, player, Platform.CONSOLE, 8_000, "futbin", now)
        repo.record_market_state(
            session, player, Platform.CONSOLE, "futbin", now, (8_000, 8_100), 150, 50_000
        )
    engine.dispose()

    result = runner.invoke(app, ["analyze", "1"])
    assert result.exit_code == 0, result.output
    assert "8.000" in result.output
    assert "Kauf-Dip" in result.output

    result = runner.invoke(app, ["signals"])
    assert result.exit_code == 0, result.output
    assert "Kauf-Dip" in result.output
    assert "Keine Signale." in runner.invoke(app, ["signals", "--rule", "sell_target"]).output
    assert runner.invoke(app, ["signals", "--rule", "nope"]).exit_code != 0
    assert runner.invoke(app, ["analyze", "999"]).exit_code == 1


def test_alert_commands_without_channel(db_path: Path) -> None:
    result = runner.invoke(app, ["alert", "test"])
    assert result.exit_code == 0, result.output
    assert "sent via log" in result.output
    result = runner.invoke(app, ["alert", "check"])
    assert result.exit_code == 0, result.output
    assert "Keine Alert-Kandidaten." in result.output


def test_watch_add_with_only_futbin_link(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fcast.sources.base import PlayerInfo
    from fcast.sources.futbin import FutbinSource

    async def fake_resolve(self: FutbinSource, ref: str) -> PlayerInfo:
        assert ref == "/27/player/22947/michael-olise"
        return PlayerInfo(ea_id=50579475, name="Michael Olise", rating=91, chem_style="Hunter")

    monkeypatch.setattr(FutbinSource, "resolve_card", fake_resolve)
    result = runner.invoke(
        app, ["watch", "add", "--futbin", "https://www.futbin.com/27/player/22947/michael-olise"]
    )
    assert result.exit_code == 0, result.output
    assert "Michael Olise (91) [50579475]" in result.output

    mismatch = runner.invoke(
        app,
        [
            "watch",
            "add",
            "231747",
            "--futbin",
            "https://www.futbin.com/27/player/22947/michael-olise",
        ],
    )
    assert mismatch.exit_code == 1
    assert "belongs to card 50579475" in mismatch.output

    assert runner.invoke(app, ["watch", "add"]).exit_code != 0


def test_promo_add_and_list(db_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "promo", "add", "Future Stars", "--start", "2030-10-09", "--end", "2030-10-16",
            "--player", "Michael Olise", "--league", "Bundesliga", "--confidence", "0.8",
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert "saved with 2 link(s)" in result.output
    assert "Future Stars" in runner.invoke(app, ["promo", "list"]).output
    from fcast.db import repositories as repo
    from fcast.db.session import create_db_engine, create_session_factory

    engine = create_db_engine(get_settings().db_url)
    with create_session_factory(engine)() as session:
        (promo,) = repo.list_promos(session)
        links = {(link.link_type.value, link.link_value) for link in promo.links}
    engine.dispose()
    assert links == {("player", "michael-olise"), ("league", "Bundesliga")}
    assert runner.invoke(app, ["promo", "add", "X", "--start", "2030-01-01"]).exit_code != 0
    assert (
        runner.invoke(app, ["promo", "add", "X", "--start", "bad", "--league", "L"]).exit_code != 0
    )


def test_backtest_command(db_path: Path) -> None:
    from datetime import UTC, datetime, timedelta

    from fcast.db import repositories as repo
    from fcast.db.session import create_db_engine, create_session_factory

    assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
    engine = create_db_engine(get_settings().db_url)
    start = datetime(2026, 9, 1, tzinfo=UTC)
    with create_session_factory(engine).begin() as session:
        player = repo.upsert_player(session, 1, repo.PlayerDetails(name="Dip", rating=85))
        prices = [10_000] * 168 + [8_500] + [10_000] * 5
        for hour, price in enumerate(prices):
            at = start + timedelta(hours=hour)
            repo.add_snapshot(session, player, get_settings().platform, price, "futbin", at)
    engine.dispose()

    args = ["backtest", "--from", "2026-09-01", "--to", "2026-09-20", "--sweep", "dip_pct=10,20"]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert "Backtest BUY_DIP" in result.output
    assert "905" in result.output  # profit of the one dip trade
    assert "Sweep dip_pct" in result.output

    result = runner.invoke(app, ["backtest", "--rule", "HOLO_SPREAD"])
    assert result.exit_code != 0
    result = runner.invoke(app, ["backtest", "--sweep", "threshold=1,2"])
    assert result.exit_code != 0


def test_lage_command(db_path: Path) -> None:
    import json
    from datetime import UTC, datetime, timedelta

    from fcast.db import repositories as repo
    from fcast.db.session import create_db_engine, create_session_factory

    assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
    engine = create_db_engine(get_settings().db_url)
    now = datetime.now(UTC)
    platform = get_settings().platform
    with create_session_factory(engine).begin() as session:
        player = repo.upsert_player(session, 7, repo.PlayerDetails(name="Doku", rating=84))
        repo.set_watch(session, player, note="ÜV Gold")
        for hours in range(100, 0, -1):
            at = now - timedelta(hours=hours)
            repo.add_snapshot(session, player, platform, 60_000, "futbin", at)
        repo.add_snapshot(session, player, platform, 54_000, "futbin", now)
        repo.record_market_state(
            session, player, platform, "futbin", now, (54_000, 59_500), 10_000, 150_000
        )
    engine.dispose()

    result = runner.invoke(app, ["lage"])
    assert result.exit_code == 0, result.output
    assert "Marktlage" in result.output
    assert "Doku (84)" in result.output
    assert "Kauf-Dip" in result.output  # 10 % under the 7-day mean
    assert "Backtest" in result.output

    result = runner.invoke(app, ["lage", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    card = data["cards"][0]
    assert (card["name"], card["price"], card["note"]) == ("Doku (84)", 54_000, "ÜV Gold")
    assert card["listings"] == [54_000, 59_500]
    assert card["cheapest_hour"] is not None  # more than 3 days of data
    assert data["mood"]["cards"] == 1


def test_watch_interval_and_remove(db_path: Path) -> None:
    runner.invoke(app, ["watch", "add", "231747", "--name", "Mbappé"])
    result = runner.invoke(app, ["watch", "interval", "231747", "120"])
    assert result.exit_code == 0, result.output
    assert "2 h" in result.output
    listing = runner.invoke(app, ["watch", "list"], env={"COLUMNS": "200"}).output
    assert "2 h" in listing
    assert "every run" in runner.invoke(app, ["watch", "interval", "231747", "0"]).output

    assert runner.invoke(app, ["watch", "interval", "1", "60"]).exit_code == 1
    result = runner.invoke(app, ["watch", "remove", "231747"])
    assert result.exit_code == 0, result.output
    assert "Watchlist is empty" in runner.invoke(app, ["watch", "list"]).output
