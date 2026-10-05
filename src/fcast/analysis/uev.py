"""ÜV shopping list: the most played cards that are still cheap to buy.

The collected market price is the cheapest buy-now offer, which usually carries no chemistry
style. A copy with a popular style is worth more to a buyer, so the idea is to buy such a copy
close to the base price (up to `premium` coins more) and list it above the break-even price.
Which offers carry a style is not in the price data; finding them stays manual.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from itertools import groupby

from fcast.analysis.cards import CardValue
from fcast.analysis.pricing import break_even_sell_price, round_to_price_step


@dataclass(frozen=True)
class UevCandidate:
    player_id: int
    rating: int | None
    games: int  # FUTBIN games counter since the card's release
    price: int  # cheapest buy-now offer
    max_buy: int  # highest sensible price for a styled copy
    break_even: int  # lowest listing price that covers `max_buy` after EA tax


def uev_candidates(
    values: dict[int, CardValue],
    *,
    max_price: int,
    premium: int,
    limit: int | None = None,
    rating: int | None = None,
) -> list[UevCandidate]:
    """Cards with a price and usage data up to `max_price`, most played first."""
    found = []
    for value in values.values():
        if value.price is None or not value.games or value.price > max_price:
            continue
        if rating is not None and value.rating != rating:
            continue
        max_buy = round_to_price_step(value.price + premium, "down")
        found.append(
            UevCandidate(
                value.player_id,
                value.rating,
                value.games,
                value.price,
                max_buy,
                break_even_sell_price(max_buy),
            )
        )
    found.sort(key=lambda c: (-c.games, c.price, c.player_id))
    return found if limit is None else found[:limit]


def by_rating(
    candidates: Iterable[UevCandidate], limit: int
) -> list[tuple[int | None, list[UevCandidate]]]:
    """Group by rating (highest first, unknown last), each group most played first."""

    def rating_key(c: UevCandidate) -> int:
        return -(c.rating if c.rating is not None else -1)

    ordered = sorted(candidates, key=lambda c: (rating_key(c), -c.games, c.price, c.player_id))
    return [
        (group[0].rating, group[:limit])
        for group in (list(g) for _, g in groupby(ordered, key=rating_key))
    ]
