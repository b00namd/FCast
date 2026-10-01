"""Early signals of the potential radar. Pure functions on time series.

- TREND_START: the price has been rising steadily for some hours but has not jumped yet.
- SUPPLY_SHRINKING: the number of listings drops (towards extinct) while the price holds.
- USAGE_SURGE: the card is played much more than before (FUTBIN games counter) and more than
  most cards - demand tends to come before the price.
- FODDER_RISE: the cheapest cards of a rating get more expensive (a big SBC is coming).

Scores are 0-100; a card's potential is its strongest signal plus a bonus for each further one.
"""

import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from itertools import pairwise

from fcast.analysis.stats import Point
from fcast.config import Settings

FULL_LISTINGS = 5  # FUTBIN shows at most five lowest BINs


class RadarKind(StrEnum):
    TREND_START = "TREND_START"
    SUPPLY_SHRINKING = "SUPPLY_SHRINKING"
    USAGE_SURGE = "USAGE_SURGE"
    FODDER_RISE = "FODDER_RISE"


LABELS = {
    RadarKind.TREND_START: "Trend-Start",
    RadarKind.SUPPLY_SHRINKING: "Angebot schrumpft",
    RadarKind.USAGE_SURGE: "Nutzung steigt",
    RadarKind.FODDER_RISE: "Futter zieht an",
}


@dataclass(frozen=True)
class RadarSignal:
    kind: RadarKind
    score: float
    reasons: tuple[str, ...]

    @property
    def label(self) -> str:
        return LABELS[self.kind]


@dataclass(frozen=True)
class RadarConfig:
    trend_window_h: float = 12.0
    trend_min_points: int = 4
    trend_min_pct: float = 5.0
    trend_max_pct: float = 30.0  # above this the jump has already happened
    trend_consistency: float = 0.75  # share of steps that did not go down
    supply_window_h: float = 24.0
    supply_min_drop: int = 2
    usage_window_h: float = 24.0
    usage_min_per_day: int = 500
    usage_accel: float = 1.5  # games per day now vs. the day before
    fodder_window_h: float = 24.0
    fodder_min_pct: float = 8.0
    alert_score: float = 70.0

    @classmethod
    def from_settings(cls, settings: Settings) -> "RadarConfig":
        return cls(
            trend_min_pct=settings.radar_trend_min_pct,
            usage_accel=settings.radar_usage_accel,
            fodder_min_pct=settings.radar_fodder_min_pct,
            alert_score=settings.radar_alert_score,
        )


def _pct(value: float) -> str:
    return f"{value:+.1f} %".replace(".", ",")


def _dec(value: float) -> str:
    return f"{value:.1f}".replace(".", ",")


def _coins(value: float) -> str:
    return f"{round(value):,}".replace(",", ".")


def _window(points: Sequence[Point], since: datetime, until: datetime) -> list[Point]:
    return [(at, price) for at, price in points if since <= at <= until]


def trend_start(points: Sequence[Point], now: datetime, cfg: RadarConfig) -> RadarSignal | None:
    """Steady rise over the last hours, still close to its high, not yet a big jump."""
    window = _window(points, now - timedelta(hours=cfg.trend_window_h), now)
    if len(window) < cfg.trend_min_points:
        return None
    first, last = window[0][1], window[-1][1]
    rise = (last - first) / first * 100
    if not cfg.trend_min_pct <= rise <= cfg.trend_max_pct:
        return None
    steps = list(pairwise(window))
    holding = sum(1 for (_, a), (_, b) in steps if b >= a)
    consistency = holding / len(steps)
    if consistency < cfg.trend_consistency:
        return None
    if last < max(price for _, price in window) * 0.98:
        return None  # already falling back from a spike
    hours = (window[-1][0] - window[0][0]).total_seconds() / 3600
    score = 50 + 30 * min(rise / 15, 1.0) + 20 * consistency
    return RadarSignal(
        RadarKind.TREND_START,
        round(min(score, 100.0), 1),
        (
            f"{_pct(rise)} in {hours:.0f} h ({_coins(first)} → {_coins(last)})",
            f"{holding} von {len(steps)} Abfragen ohne Rückgang",
        ),
    )


def supply_shrinking(
    observations: Sequence[tuple[datetime, int]],
    points: Sequence[Point],
    now: datetime,
    cfg: RadarConfig,
) -> RadarSignal | None:
    """Listings dropping within the window (towards extinct) while the price does not fall."""
    since = now - timedelta(hours=cfg.supply_window_h)
    window = [(at, count) for at, count in observations if since <= at <= now]
    if len(window) < 3:
        return None
    first, last = window[0][1], window[-1][1]
    if first - last < cfg.supply_min_drop or last >= FULL_LISTINGS:
        return None
    prices = _window(points, since, now)
    if len(prices) >= 2 and prices[-1][1] < prices[0][1] * 0.98:
        return None  # cheaper listings sold out because the price is falling
    hours = (window[-1][0] - window[0][0]).total_seconds() / 3600
    reasons = [f"Angebote {first} → {last} in {hours:.0f} h"]
    score = 40 + 12 * (first - last)
    if last == 0:
        score += 15
        reasons.append("jetzt extinct")
    if len(prices) >= 2:
        change = (prices[-1][1] - prices[0][1]) / prices[0][1] * 100
        reasons.append(f"Preis {_pct(change)}")
    return RadarSignal(RadarKind.SUPPLY_SHRINKING, round(min(score, 100.0), 1), tuple(reasons))


def usage_rates(
    usage: Sequence[tuple[datetime, int]], now: datetime, window_h: float
) -> tuple[float | None, float | None]:
    """Games per day in the last window and in the window before (None without data)."""

    def rate(start: datetime, end: datetime) -> float | None:
        inside = [(at, games) for at, games in usage if start <= at <= end]
        if len(inside) < 2:
            return None
        span = (inside[-1][0] - inside[0][0]).total_seconds() / 86400
        if span < window_h / 24 / 2:  # need at least half the window covered
            return None
        return (inside[-1][1] - inside[0][1]) / span

    window = timedelta(hours=window_h)
    return rate(now - window, now), rate(now - 2 * window, now - window)


def usage_surge(
    current: float | None,
    previous: float | None,
    cohort_median: float | None,
    cfg: RadarConfig,
) -> RadarSignal | None:
    """Played clearly more than the day before, and at least as much as the typical card."""
    if current is None or previous is None or previous <= 0:
        return None
    if current < cfg.usage_min_per_day:
        return None
    accel = current / previous
    if accel < cfg.usage_accel:
        return None
    if cohort_median is not None and current < cohort_median:
        return None
    reasons = [f"{_coins(current)} Spiele/Tag statt {_coins(previous)} (Faktor {_dec(accel)})"]
    score = 40 + 40 * min((accel - 1) / 2, 1.0)
    if cohort_median:
        ratio = current / cohort_median
        reasons.append(f"{_dec(ratio)}-mal so viel wie der Schnitt")
        if ratio >= 2:
            score += 10
    return RadarSignal(RadarKind.USAGE_SURGE, round(min(score, 100.0), 1), tuple(reasons))


def cohort_median(rates: Sequence[float | None]) -> float | None:
    known = [r for r in rates if r is not None and r > 0]
    return statistics.median(known) if len(known) >= 3 else None


@dataclass(frozen=True)
class FodderLine:
    rating: int
    price: int
    change_pct: float | None  # vs. the window before
    signal: RadarSignal | None


def fodder_lines(
    history: dict[int, Sequence[Point]], now: datetime, cfg: RadarConfig
) -> list[FodderLine]:
    """Current fodder price per rating and its change over the window."""
    lines = []
    for rating in sorted(history):
        series = sorted(history[rating])
        if not series:
            continue
        price = series[-1][1]
        cutoff = now - timedelta(hours=cfg.fodder_window_h)
        # The reference is the last price before the window, if it is not much older.
        before = [p for at, p in series if cutoff - timedelta(hours=12) <= at <= cutoff]
        change = (price - before[-1]) / before[-1] * 100 if before else None
        signal = None
        if change is not None and change >= cfg.fodder_min_pct:
            signal = RadarSignal(
                RadarKind.FODDER_RISE,
                round(min(40 + 50 * min(change / 30, 1.0), 100.0), 1),
                (f"{rating}er {_pct(change)} in {cfg.fodder_window_h:.0f} h → {_coins(price)}",),
            )
        lines.append(FodderLine(rating, price, change, signal))
    return lines


def potential(signals: Sequence[RadarSignal]) -> float:
    """Strongest signal plus 5 points for each further one."""
    if not signals:
        return 0.0
    best = max(s.score for s in signals)
    return round(min(best + 5 * (len(signals) - 1), 100.0), 1)
