"""FUTNext price source (HTML player pages), used as fallback behind FUTBIN.

Pages are addressed by EA card id (`/players/<any-slug>/<ea_id>`). The platform is a site
preference stored in the `settings` cookie; without it the page shows console prices.
Prices are rounded ("4.99M"), and there is no "last updated" time, so quotes are timestamped
with the fetch time.

Terms of service restrict copying; the user accepted that risk (see CLAUDE.md, docs/sources.md).
"""

import json
import re
from decimal import Decimal
from urllib.parse import quote

from selectolax.parser import HTMLParser

from fcast.config import Platform
from fcast.db.base import utcnow
from fcast.sources.base import (
    NoPriceError,
    PlayerInfo,
    PriceQuote,
    PriceSource,
    SourceError,
)
from fcast.sources.http import PoliteHttpClient

SOURCE_NAME = "futnext"
BASE_URL = "https://www.futnext.com"

_PLATFORM_VALUES = {Platform.CONSOLE: "ps", Platform.PC: "pc"}
_PLATFORM_LABELS = {Platform.CONSOLE: "PS / XB", Platform.PC: "PC"}
_PRICE_RE = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*([KkMm]?)\s*$")
_TITLE_RE = re.compile(r"^(?P<name>.+?)\s+(?P<rating>\d{2,3})\s+(?P<type>.+?)\s+—\s+Price")
_MULTIPLIERS = {"": 1, "k": 1_000, "m": 1_000_000}


class FutnextFormatError(SourceError):
    """The page layout changed and the parser needs to be updated."""


def player_url(ea_id: int) -> str:
    return f"{BASE_URL}/players/p/{ea_id}"


def platform_cookie(platform: Platform) -> dict[str, str]:
    settings = {"state": {"platform": _PLATFORM_VALUES[platform]}, "version": 0}
    return {"settings": quote(json.dumps(settings, separators=(",", ":")))}


def parse_coins(text: str) -> int | None:
    """'4.99M' -> 4_990_000, '45.5K' -> 45_500, '950' -> 950; None if not a price."""
    match = _PRICE_RE.match(text)
    if match is None:
        return None
    value = Decimal(match[1].replace(",", ".")) * _MULTIPLIERS[match[2].lower()]
    return int(value)


def parse_price(html: str, platform: Platform) -> int:
    tree = HTMLParser(html)
    for label in tree.css("span"):
        if label.text(strip=True) != "Price" or label.parent is None:
            continue
        parts = [
            node.text(strip=True)
            for node in label.parent.traverse(include_text=False)
            if node is not label and node.text(deep=False, strip=True)
        ]
        texts = [part for part in parts if part]
        if not texts:
            continue
        if _PLATFORM_LABELS[platform] not in texts:
            raise FutnextFormatError(f"page does not show {platform} prices (platform cookie?)")
        for text in texts:
            price = parse_coins(text)
            if price is not None:
                if price <= 0:
                    raise NoPriceError(f"no {platform} price listed")
                return price
        raise NoPriceError(f"no {platform} price listed")
    raise FutnextFormatError("no price element found")


def parse_player(html: str, ea_id: int) -> PlayerInfo:
    tree = HTMLParser(html)
    title = tree.css_first("title")
    match = _TITLE_RE.match(title.text(strip=True)) if title is not None else None
    if match is None:
        raise FutnextFormatError("unexpected page title")
    return PlayerInfo(
        ea_id=ea_id,
        name=match["name"],
        rating=int(match["rating"]),
        card_type=match["type"],
    )


class FutnextSource(PriceSource):
    name = SOURCE_NAME
    remote = True

    def __init__(self, client: PoliteHttpClient, platform: Platform) -> None:
        self._client = client
        self._platform = platform  # used for detail lookups so they hit the page cache

    async def fetch_price(self, ea_id: int, platform: Platform) -> PriceQuote:
        html = await self._client.get_text(player_url(ea_id), platform_cookie(platform))
        return PriceQuote(
            ea_id=ea_id,
            platform=platform,
            price=parse_price(html, platform),
            source=self.name,
            captured_at=utcnow(),
        )

    async def fetch_player(self, ea_id: int) -> PlayerInfo:
        html = await self._client.get_text(player_url(ea_id), platform_cookie(self._platform))
        return parse_player(html, ea_id)

    async def aclose(self) -> None:
        await self._client.aclose()
