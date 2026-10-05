"""Shopping list for ÜV ("überteuert verkaufen").

The market price FCast collects is the cheapest buy-now offer, and those copies usually carry
no chemistry style. A copy that already has the popular style is worth more to a buyer, so the
trade is to find one close to the plain market price, pay at most a small premium for it and
list it again with a surcharge. This module answers the first half: which cards are played
often enough to be worth the attempt, and how much they may cost.

Which offers actually carry a style is not in the price data - that part stays manual.
"""

from dataclasses import dataclass

from fcast.analysis.cards import CardValue
from fcast.analysis.pricing import break_even_sell_price


@dataclass(frozen=True)
class UevCandidate:
    player_id: int
    games: int  # FUTBIN games counter: how often the card is really played
    price: int  # cheapest buy-now offer, usually without a chemistry style
    max_buy: int  # highest price still worth paying for a styled copy
    break_even: int  # lowest valid listing price that covers `max_buy` after the 5 % tax
    rating: int | None = None


def uev_candidates(
    values: dict[int, CardValue],
    max_price: int,
    premium: int,
    limit: int | None = None,
    rating: int | None = None,
) -> list[UevCandidate]:
    """Most-played cards up to `max_price`, most popular first.

    `premium` is what a styled copy may cost above the market price; `rating` keeps only one
    card rating.
    """
    candidates = [
        UevCandidate(
            player_id=value.player_id,
            games=value.games,
            price=value.price,
            max_buy=value.price + premium,
            break_even=break_even_sell_price(value.price + premium),
            rating=value.rating,
        )
        for value in values.values()
        if value.games is not None
        and value.price is not None
        and value.price <= max_price
        and (rating is None or value.rating == rating)
    ]
    candidates.sort(key=lambda candidate: (-candidate.games, candidate.player_id))
    return candidates[:limit] if limit is not None else candidates


def by_rating(
    candidates: list[UevCandidate], limit: int | None = None
) -> list[tuple[int, list[UevCandidate]]]:
    """Group by card rating, best rating first; within a rating the most played come first.

    Cards without a rating are left out - they cannot be compared with the rest.
    `limit` applies per rating, not overall.
    """
    groups: dict[int, list[UevCandidate]] = {}
    for candidate in candidates:
        if candidate.rating is None:
            continue
        groups.setdefault(candidate.rating, []).append(candidate)
    return [
        (rating, group[:limit] if limit is not None else group)
        for rating, group in sorted(groups.items(), reverse=True)
    ]
