"""Event curves: how card prices moved around a promo start or a TOTW release. Pure functions.

For every card the price at each offset (hours relative to the event) is compared with the
price at the first offset (default T-72 h). Offsets without a fresh enough price are left out.
"""

import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from fcast.analysis.stats import Point
from fcast.backtest.engine import quote

OFFSETS_H: tuple[int, ...] = (-72, -48, -24, 0, 24, 48)
MAX_AGE = timedelta(hours=24)  # pool cards are priced about every 12 h


def event_curve(
    points: Sequence[Point],
    event: datetime,
    offsets: Sequence[int] = OFFSETS_H,
    max_age: timedelta = MAX_AGE,
) -> dict[int, float] | None:
    """Price change in percent per offset relative to the first offset; None without a base."""
    base = quote(points, event + timedelta(hours=offsets[0]), max_age)
    if not base:
        return None
    curve: dict[int, float] = {}
    for offset in offsets:
        price = quote(points, event + timedelta(hours=offset), max_age)
        if price is not None:
            curve[offset] = round((price - base) / base * 100, 2)
    return curve if len(curve) > 1 else None


@dataclass(frozen=True)
class CardCurve:
    ea_id: int
    name: str
    curve: dict[int, float]


@dataclass
class EventCurves:
    """All card curves of one event and group (e.g. TOTW 3, "im TOTW")."""

    kind: str  # "promo" or "totw"
    label: str
    at: datetime
    group: str
    cards: list[CardCurve] = field(default_factory=list)

    def average(self, offsets: Sequence[int] = OFFSETS_H) -> dict[int, tuple[float, int]]:
        """Mean change and number of cards per offset."""
        result: dict[int, tuple[float, int]] = {}
        for offset in offsets:
            values = [c.curve[offset] for c in self.cards if offset in c.curve]
            if values:
                result[offset] = (round(statistics.fmean(values), 2), len(values))
        return result
