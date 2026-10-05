"""Finds the FUTBIN page of a card from its EA id.

FUTBIN's search uses query strings, which robots.txt disallows. Instead FCast reads the
public player sitemaps (meant for crawlers, refreshed daily), picks pages whose name slug
matches the player, and opens candidates until the card image shows the wanted EA id.
"""

import logging
import re
import time
import unicodedata
from collections import defaultdict
from collections.abc import Callable, Sequence

from fcast.sources.futbin import BASE_URL, parse_card_id
from fcast.sources.http import PoliteHttpClient

logger = logging.getLogger(__name__)

YEAR = "27"
SITEMAP_INDEX = f"{BASE_URL}/sitemap_index.xml"
INDEX_TTL_S = 24 * 3600.0
MAX_PAGES = 6  # at most this many candidate pages per lookup (3 s apart)
SPECIAL_CARD_MIN_ID = 50_000_000  # EA ids of special cards are offset by multiples of 2^24

_LOC_RE = re.compile(r"<loc>([^<]+)</loc>")
_PLAYER_URL_RE = re.compile(rf"^{re.escape(BASE_URL)}/{YEAR}/player/(\d+)/([a-z0-9-]+)$")
_PLAYER_SITEMAP_RE = re.compile(rf"/{YEAR}/player/\d+/sitemap\.xml$")


# Letters that Unicode does not decompose into base letter + accent. "ß" is left out on
# purpose: FUTBIN drops it ("Pascal Groß" -> "pascal-gro").
_TRANSLIT = str.maketrans(
    {"ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE", "đ": "d", "Đ": "D", "ł": "l", "Ł": "L",
     "þ": "th", "Þ": "TH", "œ": "oe", "Œ": "OE",
     chr(0x131): "i"}  # dotless i (Turkish)
)  # fmt: skip


def slugify(name: str) -> str:
    """'Kylian Mbappé' -> 'kylian-mbappe', 'Martin Ødegaard' -> 'martin-odegaard'."""
    ascii_name = (
        unicodedata.normalize("NFKD", name.translate(_TRANSLIT)).encode("ascii", "ignore").decode()
    )
    return re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")


def name_hints_from_futgg(link: str) -> list[str]:
    """'…/players/231747-kylian-mbappe/27-50563395/' -> ['kylian-mbappe']."""
    match = re.search(r"fut\.gg/players/\d+-([a-z0-9-]+)/", link)
    return [match[1]] if match else []


class FutbinLocator:
    def __init__(
        self, client: PoliteHttpClient, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._client = client
        self._clock = clock
        self._index: dict[str, list[int]] = {}
        self._loaded_at: float | None = None

    async def index(self) -> dict[str, list[int]]:
        """Slug -> FUTBIN ids of all cards of the current game (cached for a day)."""
        if self._loaded_at is not None and self._clock() - self._loaded_at < INDEX_TTL_S:
            return self._index
        root = await self._client.get_text(SITEMAP_INDEX)
        index: dict[str, list[int]] = defaultdict(list)
        for sitemap in _LOC_RE.findall(root):
            if not _PLAYER_SITEMAP_RE.search(sitemap):
                continue
            for url in _LOC_RE.findall(await self._client.get_text(sitemap)):
                match = _PLAYER_URL_RE.match(url)
                if match is not None:
                    index[match[2]].append(int(match[1]))
        self._index = {slug: sorted(set(ids)) for slug, ids in index.items()}
        self._loaded_at = self._clock()
        logger.info("FUTBIN sitemap index: %d player slugs", len(self._index))
        return self._index

    @staticmethod
    def candidates(
        index: dict[str, list[int]],
        ea_id: int,
        hints: Sequence[str],
        newest_first: bool | None = None,
    ) -> list[str]:
        """Candidate page paths, most likely first.

        Without an EA id (`ea_id` 0) `newest_first` says whether a special card is wanted.
        """
        slugs: list[str] = []
        for hint in hints:
            slug = slugify(hint)
            if not slug:
                continue
            last = slug.rsplit("-", 1)[-1]
            exact = [s for s in (slug, last) if s in index]
            partial = sorted(s for s in index if s.endswith(f"-{last}") and s not in exact)
            for found in [*exact, *partial]:
                if found not in slugs:
                    slugs.append(found)
        special = ea_id >= SPECIAL_CARD_MIN_ID if newest_first is None else newest_first
        paths: list[str] = []
        for slug in slugs:
            # Base cards have the oldest FUTBIN ids, new special cards the newest.
            ids = sorted(index[slug], reverse=special)
            paths.extend(f"/{YEAR}/player/{futbin_id}/{slug}" for futbin_id in ids)
        return paths

    async def find(self, ea_id: int, hints: Sequence[str]) -> str | None:
        """FUTBIN page path of the card with `ea_id`, or None if not found."""
        paths = self.candidates(await self.index(), ea_id, hints)
        for path in paths[:MAX_PAGES]:
            html = await self._client.get_text(BASE_URL + path)
            if parse_card_id(html) == ea_id:
                logger.info("found FUTBIN page %s for card %s", path, ea_id)
                return path
        logger.info("no FUTBIN page found for card %s (%d candidates)", ea_id, len(paths))
        return None
