"""Manual price source backed by a CSV file.

Format (header row required, only `ea_id` and `price` are mandatory):

    ea_id,price,platform,captured_at,name,rating,position,card_type,league,nation,club
    231747,1250000,console,2026-09-29 18:00,Kylian Mbappé,91,ST,Gold Rare,LALIGA,France,Real Madrid

- `platform` defaults to the configured platform.
- `captured_at` is ISO 8601; without offset it is read in the configured local timezone.
  Missing values fall back to the file's modification time.
- Several rows per card are allowed (history); `fetch_price` returns the newest one.
- Prices may be written with thousands separators ("1.250.000" or "1,250,000").
"""

import asyncio
import csv
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime, tzinfo
from pathlib import Path

from fcast.config import Platform
from fcast.sources.base import PlayerInfo, PlayerNotFoundError, PriceQuote, PriceSource, SourceError

SOURCE_NAME = "manual"
REQUIRED_COLUMNS = {"ea_id", "price"}
_DETAIL_FIELDS = ("name", "position", "card_type", "league", "nation", "club")


class CsvFormatError(SourceError):
    pass


@dataclass(frozen=True)
class CsvRow:
    quote: PriceQuote
    player: PlayerInfo


def _parse_int(value: str, column: str, line: int) -> int:
    cleaned = re.sub(r"[\s.,_']", "", value)
    if not cleaned.isdigit():
        raise CsvFormatError(f"line {line}: invalid {column} {value!r}")
    return int(cleaned)


def _parse_timestamp(value: str, local_tz: tzinfo, line: int) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise CsvFormatError(f"line {line}: invalid captured_at {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=local_tz)
    return parsed.astimezone(UTC)


def parse_csv(
    path: Path, default_platform: Platform, local_tz: tzinfo, source: str = SOURCE_NAME
) -> list[CsvRow]:
    """Parse and validate the whole file. Raises `CsvFormatError` on the first bad row."""
    try:
        text = path.read_text(encoding="utf-8-sig")
        fallback_time = datetime.fromtimestamp(path.stat().st_mtime, UTC)
    except OSError as exc:
        raise SourceError(f"cannot read {path}: {exc}") from exc

    reader = csv.DictReader(text.splitlines())
    columns = {name.strip() for name in reader.fieldnames or []}
    missing = REQUIRED_COLUMNS - columns
    if missing:
        raise CsvFormatError(f"missing columns: {', '.join(sorted(missing))}")

    rows: list[CsvRow] = []
    for line, raw in enumerate(reader, start=2):
        record = {
            key.strip(): (value or "").strip() for key, value in raw.items() if key is not None
        }
        if not any(record.values()):
            continue
        ea_id = _parse_int(record["ea_id"], "ea_id", line)
        price = _parse_int(record["price"], "price", line)
        if ea_id <= 0 or price <= 0:
            raise CsvFormatError(f"line {line}: ea_id and price must be positive")

        platform_value = record.get("platform", "").lower()
        try:
            platform = Platform(platform_value) if platform_value else default_platform
        except ValueError as exc:
            raise CsvFormatError(f"line {line}: invalid platform {platform_value!r}") from exc

        timestamp = record.get("captured_at", "")
        captured_at = _parse_timestamp(timestamp, local_tz, line) if timestamp else fallback_time

        rating_value = record.get("rating", "")
        details = {field: record.get(field) or None for field in _DETAIL_FIELDS}
        player = PlayerInfo(
            ea_id=ea_id,
            rating=_parse_int(rating_value, "rating", line) if rating_value else None,
            **details,
        )
        quote = PriceQuote(
            ea_id=ea_id, platform=platform, price=price, source=source, captured_at=captured_at
        )
        rows.append(CsvRow(quote=quote, player=player))
    return rows


class ManualSource(PriceSource):
    """Reads prices from a CSV file that the user maintains by hand."""

    name = SOURCE_NAME

    def __init__(self, path: Path, default_platform: Platform, local_tz: tzinfo) -> None:
        self.path = path
        self._default_platform = default_platform
        self._local_tz = local_tz
        self._cache_key: tuple[int, int] | None = None
        self._rows: list[CsvRow] = []

    async def _load(self) -> list[CsvRow]:
        try:
            stat = await asyncio.to_thread(self.path.stat)
        except FileNotFoundError as exc:
            # No file simply means no manual prices right now.
            raise PlayerNotFoundError(f"{self.path} does not exist") from exc
        except OSError as exc:
            raise SourceError(f"cannot read {self.path}: {exc}") from exc
        key = (stat.st_mtime_ns, stat.st_size)
        if key != self._cache_key:
            self._rows = await asyncio.to_thread(
                parse_csv, self.path, self._default_platform, self._local_tz
            )
            self._cache_key = key
        return self._rows

    async def fetch_price(self, ea_id: int, platform: Platform) -> PriceQuote:
        quotes = [
            row.quote
            for row in await self._load()
            if row.quote.ea_id == ea_id and row.quote.platform == platform
        ]
        if not quotes:
            raise PlayerNotFoundError(f"no {platform} price for {ea_id} in {self.path.name}")
        return max(quotes, key=lambda quote: quote.captured_at)

    async def fetch_player(self, ea_id: int) -> PlayerInfo:
        # Merge all rows so details only need to be written once per card.
        rows = [row.player for row in await self._load() if row.player.ea_id == ea_id]
        if not rows:
            raise PlayerNotFoundError(f"player {ea_id} not in {self.path.name}")
        merged = PlayerInfo(ea_id=ea_id)
        for info in rows:
            merged = replace(merged, **{k: v for k, v in vars(info).items() if v is not None})
        return merged
