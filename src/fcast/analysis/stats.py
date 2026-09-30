"""Price statistics on time series of (timestamp, price). Pure functions, no database access.

Series may have gaps (collector downtime, source pauses); all figures only use the points
that exist. Figures that need more data than available are None instead of misleading values.
"""

import math
import statistics
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from itertools import pairwise

Point = tuple[datetime, int]

DAY = timedelta(days=1)
WEEK = timedelta(days=7)
MIN_POINTS_VOLATILITY = 3
MIN_SPAN_LIQUIDITY = timedelta(hours=6)


@dataclass(frozen=True)
class WindowStats:
    count: int
    mean: float
    median: float
    low: int
    high: int
    stdev: float  # population standard deviation


def window_stats(points: Sequence[Point], since: datetime) -> WindowStats | None:
    prices = [price for at, price in points if at >= since]
    if not prices:
        return None
    mean = statistics.fmean(prices)
    return WindowStats(
        count=len(prices),
        mean=mean,
        median=statistics.median(prices),
        low=min(prices),
        high=max(prices),
        # Float arithmetic; statistics.pstdev is exact but far too slow for backtests.
        stdev=math.sqrt(statistics.fmean([(p - mean) ** 2 for p in prices])),
    )


def price_at(points: Sequence[Point], at: datetime) -> int | None:
    """Newest price at or before `at`."""
    candidates = [(when, price) for when, price in points if when <= at]
    return max(candidates)[1] if candidates else None


def changes_per_day(points: Sequence[Point], since: datetime) -> float | None:
    """Liquidity proxy: how often the price moved per day in the window."""
    window = sorted((at, price) for at, price in points if at >= since)
    if len(window) < 2:
        return None
    span = window[-1][0] - window[0][0]
    if span < MIN_SPAN_LIQUIDITY:
        return None
    changes = sum(1 for (_, a), (_, b) in pairwise(window) if a != b)
    return changes / (span / DAY)


@dataclass(frozen=True)
class PriceStats:
    current: int | None
    current_at: datetime | None
    day: WindowStats | None
    week: WindowStats | None
    change_24h_pct: float | None
    deviation_pct: float | None  # current vs. 7-day mean
    volatility_pct: float | None  # 7-day coefficient of variation
    changes_per_day: float | None


def price_stats(points: Sequence[Point], now: datetime) -> PriceStats:
    ordered = sorted(points)
    current_at, current = ordered[-1] if ordered else (None, None)
    day = window_stats(ordered, now - DAY)
    week = window_stats(ordered, now - WEEK)

    previous = price_at(ordered, now - DAY)
    change = (current - previous) / previous * 100 if current and previous else None
    deviation = (current - week.mean) / week.mean * 100 if current and week else None
    volatility = (
        week.stdev / week.mean * 100
        if week is not None and week.count >= MIN_POINTS_VOLATILITY and week.mean
        else None
    )
    return PriceStats(
        current=current,
        current_at=current_at,
        day=day,
        week=week,
        change_24h_pct=change,
        deviation_pct=deviation,
        volatility_pct=volatility,
        changes_per_day=changes_per_day(ordered, now - WEEK),
    )


def _relative_profile(buckets: dict[int, list[int]], overall: float) -> dict[int, float]:
    return {
        key: (statistics.fmean(values) / overall - 1) * 100
        for key, values in sorted(buckets.items())
    }


def hour_profile(points: Sequence[Point], tz: tzinfo) -> dict[int, float]:
    """Average price per local hour relative to the overall mean, in percent."""
    if not points:
        return {}
    overall = statistics.fmean(price for _, price in points)
    buckets: dict[int, list[int]] = defaultdict(list)
    for at, price in points:
        buckets[at.astimezone(tz).hour].append(price)
    return _relative_profile(buckets, overall)


def weekday_profile(points: Sequence[Point], tz: tzinfo) -> dict[int, float]:
    """Average price per local weekday (0 = Monday) relative to the overall mean, in percent."""
    if not points:
        return {}
    overall = statistics.fmean(price for _, price in points)
    buckets: dict[int, list[int]] = defaultdict(list)
    for at, price in points:
        buckets[at.astimezone(tz).weekday()].append(price)
    return _relative_profile(buckets, overall)
