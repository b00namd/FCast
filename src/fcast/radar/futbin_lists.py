"""FUTBIN list pages for the market scanner: Popular, New Players, Cheapest Players (fodder).

All pages are plain paths without query strings (robots.txt disallows `/*?*`).
"""

import re

from selectolax.parser import HTMLParser

from fcast.config import Platform
from fcast.sources.futbin_locator import YEAR

POPULAR = f"/{YEAR}/popular"
LATEST = f"/{YEAR}/latest"
CHEAPEST = f"/{YEAR}/squad-building-challenges/cheapest"

_PLAYER_REF = re.compile(rf"/{YEAR}/player/\d+/[a-z0-9-]+")
_RATING_HEADER = re.compile(r"(\d{2})\s*\|\s*Rated Players")
_PRICE = re.compile(r"([\d.,]+)\s*([KM]?)", re.IGNORECASE)
# Platform columns on the cheapest page: shown only on PC / only on console.
_PLATFORM_COLUMN = {Platform.PC: "hide-not-pc", Platform.CONSOLE: "hide-not-ps"}


def player_refs(html: str, limit: int | None = None) -> list[str]:
    """FUTBIN card pages linked on a list page, in page order, without duplicates."""
    found: dict[str, None] = {}
    for link in HTMLParser(html).css(f'a[href^="/{YEAR}/player/"]'):
        href = link.attributes.get("href") or ""
        if _PLAYER_REF.fullmatch(href):
            found.setdefault(href)
            if limit is not None and len(found) >= limit:
                break
    return list(found)


def parse_short_price(text: str) -> int | None:
    """FUTBIN's short prices: "650", "1.8K", "10.25K", "2.18M", "1,500"."""
    match = _PRICE.fullmatch(text.strip())
    if match is None:
        return None
    number, unit = match[1], match[2].upper()
    if unit:
        value = float(number.replace(",", ""))
        return round(value * (1_000 if unit == "K" else 1_000_000))
    digits = number.replace(",", "").replace(".", "")
    return int(digits) if digits else None


def parse_fodder(html: str, platform: Platform) -> dict[int, list[int]]:
    """Cheapest prices per rating (ascending) for one platform from the cheapest-players page."""
    css_class = _PLATFORM_COLUMN[platform]
    result: dict[int, list[int]] = {}
    for column in HTMLParser(html).css(f".stc-player-column.{css_class}"):
        text = re.sub(r"\s+", " ", column.text(separator="|", strip=True))
        header = _RATING_HEADER.search(text.replace("| ", "|").replace(" |", "|"))
        if header is None:
            continue
        prices = []
        for segment in column.css("a.stc-player-wrapper .price-segment"):
            price = parse_short_price(segment.text(strip=True))
            if price:
                prices.append(price)
        if prices:
            result[int(header[1])] = sorted(prices)
    return result
