import os
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from fcast.config import Platform
from fcast.sources.base import PlayerNotFoundError, SourceError
from fcast.sources.manual import CsvFormatError, ManualSource, parse_csv

FIXTURE = Path(__file__).parent / "fixtures" / "manual" / "prices.csv"
BERLIN = ZoneInfo("Europe/Berlin")


@pytest.fixture
def source() -> ManualSource:
    return ManualSource(FIXTURE, Platform.CONSOLE, BERLIN)


def write_csv(tmp_path: Path, *lines: str) -> Path:
    path = tmp_path / "prices.csv"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_parse_fixture() -> None:
    rows = parse_csv(FIXTURE, Platform.CONSOLE, BERLIN)
    assert len(rows) == 4  # blank line skipped
    first = rows[0]
    assert first.quote.price == 1_250_000
    # 18:00 Berlin summer time == 16:00 UTC
    assert first.quote.captured_at == datetime(2026, 9, 28, 16, 0, tzinfo=UTC)
    assert first.player.name == "Kylian Mbappé"
    assert first.player.rating == 91
    # Missing platform falls back to the default.
    assert rows[3].quote.platform is Platform.CONSOLE


async def test_fetch_price_returns_newest_for_platform(source: ManualSource) -> None:
    quote = await source.fetch_price(231747, Platform.CONSOLE)
    assert quote.price == 1_190_000
    assert quote.source == "manual"
    pc = await source.fetch_price(231747, Platform.PC)
    assert pc.price == 1_300_000


async def test_fetch_price_unknown_player(source: ManualSource) -> None:
    with pytest.raises(PlayerNotFoundError):
        await source.fetch_price(1, Platform.CONSOLE)
    with pytest.raises(PlayerNotFoundError):
        await source.fetch_price(158023, Platform.PC)


async def test_fetch_player_merges_rows(source: ManualSource) -> None:
    info = await source.fetch_player(231747)
    assert info.name == "Kylian Mbappé"
    assert info.club == "Real Madrid"
    with pytest.raises(PlayerNotFoundError):
        await source.fetch_player(1)


async def test_file_changes_are_picked_up(tmp_path: Path) -> None:
    path = write_csv(tmp_path, "ea_id,price,captured_at", "1,1000,2026-09-29 10:00")
    src = ManualSource(path, Platform.CONSOLE, BERLIN)
    assert (await src.fetch_price(1, Platform.CONSOLE)).price == 1000

    write_csv(
        tmp_path, "ea_id,price,captured_at", "1,1000,2026-09-29 10:00", "1,900,2026-09-29 11:00"
    )
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    assert (await src.fetch_price(1, Platform.CONSOLE)).price == 900


def test_missing_timestamp_uses_file_mtime(tmp_path: Path) -> None:
    path = write_csv(tmp_path, "ea_id,price", "1,1000")
    mtime = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    os.utime(path, (mtime.timestamp(), mtime.timestamp()))
    (row,) = parse_csv(path, Platform.PC, BERLIN)
    assert row.quote.captured_at == mtime
    assert row.quote.platform is Platform.PC


@pytest.mark.parametrize(
    ("lines", "message"),
    [
        (("name,price", "X,1000"), "missing columns: ea_id"),
        (("ea_id,price", "abc,1000"), "line 2: invalid ea_id"),
        (("ea_id,price", "1,0"), "must be positive"),
        (("ea_id,price,platform", "1,100,switch"), "invalid platform"),
        (("ea_id,price,captured_at", "1,100,yesterday"), "invalid captured_at"),
    ],
)
def test_invalid_csv(tmp_path: Path, lines: tuple[str, ...], message: str) -> None:
    path = write_csv(tmp_path, *lines)
    with pytest.raises(CsvFormatError, match=message):
        parse_csv(path, Platform.CONSOLE, BERLIN)


async def test_missing_file_is_source_error(tmp_path: Path) -> None:
    src = ManualSource(tmp_path / "nope.csv", Platform.CONSOLE, BERLIN)
    with pytest.raises(SourceError):
        await src.fetch_price(1, Platform.CONSOLE)
