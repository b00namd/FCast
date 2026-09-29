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
    monkeypatch.setenv("FCAST_TELEGRAM_TOKEN", "super-secret")
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
    assert "2 players: 1 new snapshots, 0 unchanged, 1 without price" in result.output

    result = runner.invoke(app, ["collect", "--once"])
    assert "0 new snapshots, 1 unchanged" in result.output

    # Player details from the CSV are now known.
    assert "Kylian Mbappé (91)" in runner.invoke(app, ["watch", "list"]).output


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


def test_watch_add_with_futbin_link(db_path: Path) -> None:
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
