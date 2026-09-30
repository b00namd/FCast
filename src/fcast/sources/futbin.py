"""FUTBIN price source (HTML player pages).

FUTBIN identifies cards by its own id plus a name slug, e.g. `/27/player/21487/maradona`,
so every watched card needs a stored FUTBIN reference (see `SourceRef`).

Terms of service forbid scraping; the user accepted that risk (see CLAUDE.md, docs/sources.md).
robots.txt, rate limits and an honest User-Agent are still enforced by `PoliteHttpClient`.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from selectolax.parser import HTMLParser, Node

from fcast.config import Platform
from fcast.db.base import utcnow
from fcast.sources.base import (
    ExtinctError,
    MarketInfo,
    PlayerInfo,
    PlayerNotFoundError,
    PriceQuote,
    PriceSource,
    SourceError,
)
from fcast.sources.http import PoliteHttpClient

SOURCE_NAME = "futbin"
BASE_URL = "https://www.futbin.com"

_PATH_RE = re.compile(r"^/(?P<year>\d{2})/player/(?P<id>\d+)/(?P<slug>[^/?#\s]+)/?$")
_UPDATED_RE = re.compile(r"(\d+)\s*(sec|min|hour|day|week)s?\s*ago", re.IGNORECASE)
_RANGE_RE = re.compile(r"Price Range:\s*([\d,.]+)\s*-\s*([\d,.]+)", re.IGNORECASE)
_RATING_RE = re.compile(r"-\s*(\d{2,3})\s+rating", re.IGNORECASE)
_CARD_TYPE_RE = re.compile(r"^.+?\s{2,}-\s*EA FC")
_SHORT_NAME_RE = re.compile(r"^A headshot of (.+) in FC\d+$")
_POSITION_CLASS_RE = re.compile(r"^playercard-\d+-position$")
_PLATFORM_CLASSES = {Platform.CONSOLE: "platform-ps-only", Platform.PC: "platform-pc-only"}
_UNITS = {"sec": "seconds", "min": "minutes", "hour": "hours", "day": "days", "week": "weeks"}

RefLookup = Callable[[int], str | None]


class FutbinFormatError(SourceError):
    """The page layout changed and the parser needs to be updated."""


def normalize_ref(value: str) -> str:
    """Accept a FUTBIN player URL or path and return the canonical path."""
    path = re.sub(r"^https?://(www\.)?futbin\.com", "", value.strip())
    path = path.split("?", 1)[0].split("#", 1)[0]
    match = _PATH_RE.match(path)
    if match is None:
        raise ValueError(f"not a FUTBIN player URL: {value!r}")
    return f"/{match['year']}/player/{match['id']}/{match['slug']}"


def _parse_coins(text: str) -> int | None:
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


def _parse_updated(text: str, now: datetime) -> datetime:
    match = _UPDATED_RE.search(text)
    if match is None:
        return now
    amount, unit = int(match[1]), match[2].lower()
    return now - timedelta(**{_UNITS[unit]: amount})


def _meta(tree: HTMLParser, attr: str, value: str) -> str | None:
    node = tree.css_first(f'meta[{attr}="{value}"]')
    return node.attributes.get("content") if node is not None else None


def _link_text(tree: HTMLParser, param: str) -> str | None:
    for link in tree.css(f'a[href*="?{param}="]'):
        text = link.text(strip=True)
        if text:
            return text
    return None


def _price_box(tree: HTMLParser, platform: Platform) -> Node:
    box = tree.css_first(f".price-box.{_PLATFORM_CLASSES[platform]}.price-box-original-player")
    if box is None:
        raise FutbinFormatError(f"no {platform} price box found")
    return box


@dataclass(frozen=True)
class FutbinPrice:
    price: int  # lowest BIN
    updated: datetime
    market: MarketInfo


def parse_price(html: str, platform: Platform, now: datetime | None = None) -> FutbinPrice:
    """Lowest BINs, EA price range and update time for a platform.

    Raises `ExtinctError` (with the price range) when no listing exists.
    """
    now = now or utcnow()
    tree = HTMLParser(html)
    box = _price_box(tree, platform)
    first = box.css_first(".lowest-price-1")
    if first is None:
        raise FutbinFormatError(f"no {platform} price element found")
    values = [_parse_coins(first.text(strip=True))]
    values += [_parse_coins(node.text(strip=True)) for node in box.css(".lowest-price")]
    listings = tuple(sorted(value for value in values if value))
    text = box.text(separator=" ")
    range_match = _RANGE_RE.search(text)
    market = MarketInfo(
        listings=listings,
        range_min=_parse_coins(range_match[1]) if range_match else None,
        range_max=_parse_coins(range_match[2]) if range_match else None,
    )
    if market.extinct:
        raise ExtinctError(f"no {platform} listings (extinct)", market)
    return FutbinPrice(price=listings[0], updated=_parse_updated(text, now), market=market)


def parse_player(html: str, ea_id: int) -> PlayerInfo:
    tree = HTMLParser(html)
    title = _meta(tree, "property", "og:title")
    if not title:
        raise FutbinFormatError("no og:title found")
    description = _meta(tree, "name", "description") or ""

    rating_match = _RATING_RE.search(description)
    card_type = None
    short_name = _SHORT_NAME_RE.match(_meta(tree, "property", "og:image:alt") or "")
    type_match = _CARD_TYPE_RE.match(description)
    if type_match is not None and short_name is not None:
        # "Maradona Icon  - EA FC 27 - 95 rating" minus short name "Maradona" -> "Icon"
        head = type_match[0].rsplit("-", 1)[0].strip()
        if head.startswith(short_name[1]):
            card_type = head.removeprefix(short_name[1]).strip() or None

    position = None
    for node in tree.css('[class*="-position"]'):
        classes = (node.attributes.get("class") or "").split()
        if any(_POSITION_CLASS_RE.match(cls) for cls in classes):
            position = node.text(strip=True) or None
            break

    return PlayerInfo(
        ea_id=ea_id,
        name=title.split(" - ", 1)[0].strip(),
        rating=int(rating_match[1]) if rating_match else None,
        position=position,
        card_type=card_type,
        league=_link_text(tree, "league"),
        nation=_link_text(tree, "nation"),
        club=_link_text(tree, "club"),
    )


class FutbinSource(PriceSource):
    name = SOURCE_NAME
    remote = True

    def __init__(self, client: PoliteHttpClient, lookup: RefLookup) -> None:
        self._client = client
        self._lookup = lookup

    async def _page(self, ea_id: int) -> str:
        ref = self._lookup(ea_id)
        if ref is None:
            raise PlayerNotFoundError(f"no FUTBIN link stored for {ea_id}")
        # The client caches pages, so price and player lookups share one request.
        return await self._client.get_text(BASE_URL + ref)

    async def fetch_price(self, ea_id: int, platform: Platform) -> PriceQuote:
        parsed = parse_price(await self._page(ea_id), platform)
        return PriceQuote(
            ea_id=ea_id,
            platform=platform,
            price=parsed.price,
            source=self.name,
            captured_at=parsed.updated,
            market=parsed.market,
        )

    async def fetch_player(self, ea_id: int) -> PlayerInfo:
        return parse_player(await self._page(ea_id), ea_id)

    async def aclose(self) -> None:
        await self._client.aclose()
