"""Card values across all known cards: play value, real usage and the price they usually cost.

The "meta score" is the play value confirmed by usage (how much the card is played compared
with the other known cards). For base cards (gold, silver, bronze) a log-linear fit of price
over meta score and rating tells what a card of that quality and rating usually costs; cards
far below that are "undervalued". Special cards and holo versions are a market of their own
and are not compared.
"""

import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from fcast.analysis import playvalue as pv
from fcast.config import Settings
from fcast.db import repositories as repo
from fcast.db.models import Player
from fcast.radar.signals import usage_rates

RECENT = timedelta(days=3)
MIN_FIT_PRICE = 2_000  # cheaper cards sit at the market floor; their price says nothing
MIN_FIT_CARDS = 40  # fewer cards gave implausible curves (higher rating = cheaper)
MIN_AGREEMENT = 0.2  # the play value must demonstrably go along with real usage
MIN_RATE_PAIRS = 8
BASE_TYPES = ("gold", "silver", "bronze")


def is_base_card(card_type: str | None) -> bool:
    return (card_type or "").lower().startswith(BASE_TYPES)


def is_holo(card_type: str | None) -> bool:
    return (card_type or "").endswith("(Holo)")


@dataclass(frozen=True)
class CardValue:
    player_id: int
    play: pv.PlayValue
    meta: float
    price: int | None
    usage_rate: float | None  # games per day (last 24 h)
    games: int | None  # FUTBIN games counter since the card's release
    rating: int | None = None
    base_card: bool = False  # gold/silver/bronze base card


def card_values(session: Session, settings: Settings, now: datetime) -> dict[int, CardValue]:
    """Play value, usage and price of every card with known attributes (holo versions excluded)."""
    found: dict[int, tuple[pv.PlayValue, int | None, float | None, Player]] = {}
    for player in session.scalars(select(Player).where(Player.attributes_raw.is_not(None))):
        if is_holo(player.card_type):
            continue  # same stats as the normal card, priced and played differently
        play = pv.play_value(player.attributes, player.position)
        if play is None:
            continue
        last = repo.latest_snapshot(session, player, settings.platform)
        price = last.price if last is not None and now - last.captured_at <= RECENT else None
        usage = [(u.observed_at, u.games) for u in repo.list_usage(session, player, now - RECENT)]
        rate, _ = usage_rates(usage, now, 24)
        found[player.id] = (play, price, rate, player)
    ranks = pv.percentiles(
        {pid: rate for pid, (_, _, rate, _) in found.items() if rate is not None}
    )
    return {
        pid: CardValue(
            pid,
            play,
            pv.meta_score(play.score, ranks.get(pid)),
            price,
            rate,
            player.games_used,
            player.rating,
            is_base_card(player.card_type),
        )
        for pid, (play, price, rate, player) in found.items()
    }


@dataclass(frozen=True)
class PriceFit:
    """log(price) = intercept + meta_slope * meta + rating_slope * rating (base cards)."""

    intercept: float
    meta_slope: float
    rating_slope: float
    cards: int

    def expected(self, meta: float, rating: int) -> int:
        return round(math.exp(self.intercept + self.meta_slope * meta + self.rating_slope * rating))


def _solve3(a: list[list[float]], b: list[float]) -> list[float] | None:
    """Solve a 3x3 linear system (Gaussian elimination); None if singular."""
    m = [[*row, value] for row, value in zip(a, b, strict=True)]
    for col in range(3):
        pivot = max(range(col, 3), key=lambda r: abs(m[r][col]))
        if abs(m[pivot][col]) < 1e-9:
            return None
        m[col], m[pivot] = m[pivot], m[col]
        for row in range(3):
            if row != col:
                factor = m[row][col] / m[col][col]
                m[row] = [x - factor * y for x, y in zip(m[row], m[col], strict=True)]
    return [m[i][3] / m[i][i] for i in range(3)]


def price_fit(values: dict[int, CardValue]) -> PriceFit | None:
    """What base cards of a given meta score and rating usually cost (least squares)."""
    rows = [
        (v.meta, float(v.rating), math.log(v.price))
        for v in values.values()
        if v.base_card and v.rating and v.price is not None and v.price >= MIN_FIT_PRICE
    ]
    if len(rows) < MIN_FIT_CARDS:
        return None
    xtx = [[0.0] * 3 for _ in range(3)]
    xty = [0.0] * 3
    for meta, rating, log_price in rows:
        x = (1.0, meta, rating)
        for i in range(3):
            xty[i] += x[i] * log_price
            for j in range(3):
                xtx[i][j] += x[i] * x[j]
    beta = _solve3(xtx, xty)
    if beta is None or beta[1] <= 0 or beta[2] < 0:
        return None  # better or higher rated cards are not more expensive: implausible
    return PriceFit(beta[0], beta[1], beta[2], len(rows))


def expected_price(fit: PriceFit | None, value: CardValue) -> int | None:
    if fit is None or not value.base_card or not value.rating:
        return None
    return fit.expected(value.meta, value.rating)


@dataclass(frozen=True)
class Agreement:
    correlation: float | None
    cards: int
    basis: str  # what the play value was compared with


def usage_residuals(values: dict[int, CardValue]) -> dict[int, float]:
    """How much more a base field card is played than its price suggests (log scale).

    Cheap cards are owned and played by many more people, so the games counter mostly mirrors
    the price. The residual of log(games) over log(price) is the usage that quality explains.
    """
    cards = [
        v
        for v in values.values()
        if v.base_card and v.games and v.price and v.play.group != pv.KEEPER
    ]
    if len(cards) < MIN_RATE_PAIRS:
        return {}
    log_price = [math.log(v.price or 1) for v in cards]
    log_games = [math.log(v.games or 1) for v in cards]
    try:
        slope, intercept = statistics.linear_regression(log_price, log_games)
    except statistics.StatisticsError:
        return {}
    return {
        v.player_id: g - (intercept + slope * p)
        for v, p, g in zip(cards, log_price, log_games, strict=True)
    }


def agreement(values: dict[int, CardValue]) -> Agreement:
    """How well the play value agrees with real usage (Spearman rank correlation).

    Preferred: games per day. Until there is enough history: total games of base field cards
    with the price taken out (special cards are younger, cheap cards are owned by more people).
    """
    rates = [(v.play.score, v.usage_rate) for v in values.values() if v.usage_rate]
    if len(rates) >= MIN_RATE_PAIRS:
        pairs = [(p, float(r)) for p, r in rates if r is not None]
        return Agreement(pv.usage_agreement(pairs), len(pairs), "Spiele pro Tag")
    residuals = usage_residuals(values)
    pairs = [(values[pid].play.score, r) for pid, r in residuals.items()]
    return Agreement(
        pv.usage_agreement(pairs), len(pairs), "Spiele gesamt, Preis herausgerechnet (Gold)"
    )


def stat_drivers(
    values: dict[int, CardValue], stats: dict[int, dict[str, int]]
) -> list[tuple[str, float, int]]:
    """Which single stats go along with price-adjusted usage: (stat, correlation, cards)."""
    residuals = usage_residuals(values)
    names = sorted({name for pid in residuals for name in stats.get(pid, {})})
    found = []
    for name in names:
        pairs = [
            (float(stats[pid][name]), r)
            for pid, r in residuals.items()
            if name in stats.get(pid, {})
        ]
        corr = pv.usage_agreement(pairs)
        if corr is not None:
            found.append((name, corr, len(pairs)))
    return sorted(found, key=lambda item: -item[1])


def play_value_validated(values: dict[int, CardValue]) -> bool:
    """True once the play value is shown to go along with real usage."""
    check = agreement(values)
    return check.correlation is not None and check.correlation >= MIN_AGREEMENT
