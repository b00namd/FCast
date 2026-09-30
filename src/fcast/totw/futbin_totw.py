"""The actual Team of the Week from FUTBIN (/27/totw/TOTW<n>) for the hit rate."""

import re
from dataclasses import dataclass

from selectolax.parser import HTMLParser

from fcast.sources.futbin import BASE_URL
from fcast.sources.futbin_locator import YEAR

_PLAYER_LINK_RE = re.compile(rf"^/{YEAR}/player/(\d+)/([a-z0-9-]+)$")


@dataclass(frozen=True)
class TotwPlayer:
    futbin_id: int
    slug: str
    name: str


def totw_path(number: int) -> str:
    return f"/{YEAR}/totw/TOTW{number}"


def totw_url(number: int) -> str:
    return BASE_URL + totw_path(number)


def parse_totw_page(html: str) -> list[TotwPlayer]:
    """Players of a TOTW page, in page order, without duplicates."""
    tree = HTMLParser(html)
    seen: set[int] = set()
    players: list[TotwPlayer] = []
    for link in tree.css(f'a[href^="/{YEAR}/player/"]'):
        match = _PLAYER_LINK_RE.match(link.attributes.get("href") or "")
        if match is None:
            continue
        futbin_id = int(match[1])
        if futbin_id in seen:
            continue
        seen.add(futbin_id)
        name = re.sub(r"\s+", " ", link.text(separator=" ", strip=True)).strip()
        players.append(TotwPlayer(futbin_id=futbin_id, slug=match[2], name=name or match[2]))
    return players
