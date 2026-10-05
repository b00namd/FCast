"""Find a card from what a transfer-list screenshot shows: name, rating, card type.

Queries look like "Musiala 87", "Olise 91 TOTW" or an EA id. Words before the rating are the
name, words after it the card type. Known cards are searched in the database first; unknown
ones via FUTBIN's player sitemap (search URLs with query strings are disallowed by robots.txt).
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from fcast.analysis.cards import is_base_card, is_holo
from fcast.db.models import Player
from fcast.sources import futbin
from fcast.sources.base import PlayerInfo
from fcast.sources.futbin_locator import MAX_PAGES, slugify

# Short forms as written on cards or in chat -> words of FUTBIN's card type.
TYPE_ALIASES = {
    "totw": "team of the week",
    "if": "team of the week",
    "potm": "player of the month",
    "sbc": "sbc",
    "icon": "icon",
    "hero": "hero",
}


BASE_TYPE_SLUGS = frozenset({"", "gold", "rare", "gold-rare", "common", "gold-common"})


class CardLookupError(Exception):
    """No card or more than one card matches; the message lists what was found."""


@dataclass(frozen=True)
class CardQuery:
    text: str
    name: str
    rating: int | None = None
    card_type: str | None = None
    ea_id: int | None = None

    @property
    def holo(self) -> bool:
        return "holo" in (self.card_type or "").lower()


def parse_query(text: str) -> CardQuery:
    """'Musiala 87' -> name 'Musiala', rating 87; '231747' -> EA id."""
    value = " ".join(text.split())
    if value.isdigit():
        return CardQuery(value, "", ea_id=int(value))
    words = value.split(" ")
    for i, word in enumerate(words):
        if re.fullmatch(r"\d{2}", word) and 40 <= int(word) <= 99 and i > 0:
            card_type = " ".join(words[i + 1 :]) or None
            return CardQuery(value, " ".join(words[:i]), int(word), card_type)
    return CardQuery(value, value)


def _type_slug(card_type: str) -> str:
    words = [TYPE_ALIASES.get(word.lower(), word) for word in card_type.split()]
    return slugify(" ".join(word for word in words if word.lower() != "holo"))


def _contains(haystack: str | None, needle: str) -> bool:
    return bool(needle) and f"-{needle}-" in f"-{slugify(haystack or '')}-"


def matches(query: CardQuery, name: str | None, rating: int | None, card_type: str | None) -> bool:
    if not _contains(name, slugify(query.name)):
        return False
    if query.rating is not None and rating != query.rating:
        return False
    if query.card_type is not None:
        wanted = _type_slug(query.card_type)
        if wanted and not _contains(card_type, wanted):
            return False
    return query.holo == is_holo(card_type)


def pick(query: CardQuery, cards: Sequence[Player]) -> Player:
    """The one card meant by the query; a lone base card wins if no type was given."""
    if len(cards) == 1:
        return cards[0]
    if not cards:
        raise CardLookupError(f"no card matches {query.text!r}")
    if query.card_type is None:
        base = [card for card in cards if is_base_card(card.card_type)]
        if len(base) == 1:
            return base[0]
    found = ", ".join(
        f"{card.display_name} {card.card_type or ''}".strip() + f" [{card.ea_id}]" for card in cards
    )
    raise CardLookupError(
        f"{query.text!r} is ambiguous: {found} - add the card type or use the EA id"
    )


def find_local(session: Session, query: CardQuery) -> list[Player]:
    if query.ea_id is not None:
        player = session.scalar(select(Player).where(Player.ea_id == query.ea_id))
        return [player] if player is not None else []
    return [
        player
        for player in session.scalars(select(Player).where(Player.name.is_not(None)))
        if matches(query, player.name, player.rating, player.card_type)
    ]


@dataclass(frozen=True)
class OnlineMatch:
    path: str  # FUTBIN page, stored as source ref
    info: PlayerInfo


async def search_futbin(
    source: futbin.FutbinSource, query: CardQuery, max_pages: int = MAX_PAGES
) -> list[OnlineMatch]:
    """Open candidate pages from the sitemap until a card matches (at most `max_pages`)."""
    locator = source.locator()
    wanted = _type_slug(query.card_type) if query.card_type is not None else ""
    special = wanted not in BASE_TYPE_SLUGS  # special cards have the newest FUTBIN ids
    paths = locator.candidates(await locator.index(), 0, [query.name], newest_first=special)
    found: list[OnlineMatch] = []
    for path in paths[:max_pages]:
        html = await source.get_page(path)
        ea_id = futbin.parse_card_id(html)
        if ea_id is None:
            continue
        info = futbin.parse_player(html, ea_id)
        card_type = info.card_type
        if futbin.is_holo_page(html):
            card_type = f"{card_type or ''} (Holo)".strip()
        if matches(query, info.name, info.rating, card_type):
            found.append(OnlineMatch(path, info))
            break  # every further page costs another request
    return found
