"""Card values across all known cards: play value, real usage and the price they usually cost.

The "meta score" is the play value confirmed by usage (how much the card is played compared
with the other known cards). A log-linear fit of price over meta score tells what a card of a
given quality usually costs; cards far below that are "undervalued".
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
MIN_FIT_CARDS = 15


@dataclass(frozen=True)
class CardValue:
    player_id: int
    play: pv.PlayValue
    meta: float
    price: int | None
    usage_rate: float | None  # games per day (last 24 h)
    games: int | None  # FUTBIN games counter


def card_values(session: Session, settings: Settings, now: datetime) -> dict[int, CardValue]:
    """Play value, usage and price of every card with known attributes."""
    found: dict[int, tuple[pv.PlayValue, int | None, float | None, int | None]] = {}
    for player in session.scalars(select(Player).where(Player.attributes_raw.is_not(None))):
        play = pv.play_value(player.attributes, player.position)
        if play is None:
            continue
        last = repo.latest_snapshot(session, player, settings.platform)
        price = last.price if last is not None and now - last.captured_at <= RECENT else None
        usage = [(u.observed_at, u.games) for u in repo.list_usage(session, player, now - RECENT)]
        rate, _ = usage_rates(usage, now, 24)
        found[player.id] = (play, price, rate, player.games_used)
    ranks = pv.percentiles(
        {pid: rate for pid, (_, _, rate, _) in found.items() if rate is not None}
    )
    return {
        pid: CardValue(pid, play, pv.meta_score(play.score, ranks.get(pid)), price, rate, games)
        for pid, (play, price, rate, games) in found.items()
    }


@dataclass(frozen=True)
class PriceFit:
    intercept: float
    slope: float  # change of log(price) per meta point
    cards: int

    def expected(self, meta: float) -> int:
        return round(math.exp(self.intercept + self.slope * meta))


def price_fit(values: dict[int, CardValue]) -> PriceFit | None:
    """What cards of a given meta score usually cost (log-linear least squares)."""
    pairs = [
        (v.meta, math.log(v.price))
        for v in values.values()
        if v.price is not None and v.price >= MIN_FIT_PRICE
    ]
    if len(pairs) < MIN_FIT_CARDS:
        return None
    try:
        slope, intercept = statistics.linear_regression(
            [m for m, _ in pairs], [p for _, p in pairs]
        )
    except statistics.StatisticsError:
        return None
    if slope <= 0:
        return None  # better cards are not more expensive: the fit says nothing
    return PriceFit(intercept, slope, len(pairs))


def agreement(values: dict[int, CardValue]) -> tuple[float | None, int]:
    """How well the play value agrees with how much cards are played (Spearman, cards)."""
    pairs = [(v.play.score, float(v.games)) for v in values.values() if v.games]
    return pv.usage_agreement(pairs), len(pairs)
