"""Backtesting: replay trading rules on stored price snapshots. Pure functions, no database.

Assumptions (deterministic, so the same data always gives the same report):
- Signals only see snapshots up to their own time (no lookahead).
- Buy at the snapshot price (lowest BIN) of the snapshot where the rule fires.
- Right after buying, the card is listed at the rule's target. It counts as sold at the target
  as soon as a later snapshot is at or above it (our listing would then be the cheapest).
- Holding period or stop loss over: sell at the market, one price step below the snapshot price.
- Only snapshots inside the window are used; positions still open at its end are valued at the
  last price (market sale) and reported separately.
- 5 % EA tax on every sale. One position per card at a time.
"""

from bisect import bisect_left, bisect_right
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, fields, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from fcast.analysis import signals as sig
from fcast.analysis.pricing import net_after_tax, round_to_price_step, step_price
from fcast.analysis.stats import WEEK, Point, PriceStats, price_stats
from fcast.config import Settings
from fcast.promos.scoring import CardInfo, PromoInfo, PromoWeights, is_prebuy, score_card

MAX_QUOTE_AGE = timedelta(hours=12)  # a price older than this is no longer "the" price


class Exit(StrEnum):
    TARGET = "Ziel"
    HOLD = "Haltedauer"
    STOP = "Stop-Loss"
    EVENT = "nach Promo-Start"
    OPEN = "offen"


@dataclass(frozen=True)
class CardSeries:
    """Price history of one card, sorted by time, plus what the promo scoring needs."""

    ea_id: int
    name: str
    points: tuple[Point, ...]
    slug: str | None = None
    club: str | None = None
    league: str | None = None
    nation: str | None = None

    @property
    def info(self) -> CardInfo:
        return CardInfo(
            ea_id=self.ea_id,
            name=self.name,
            slug=self.slug,
            club=self.club,
            league=self.league,
            nation=self.nation,
        )


@dataclass(frozen=True)
class Params:
    """Rule thresholds plus trade management; every field can be swept."""

    # BUY_DIP
    dip_pct: float = 10.0
    min_margin_pct: float = 5.0
    min_profit: int = 500
    max_hold_h: float = 72.0
    stop_loss_pct: float = 0.0  # 0 = off
    # PROMO_PREBUY
    threshold: float = 45.0
    entry_days: float = 3.0  # buy this many days before the promo starts (2-7)
    exit_h: float = 24.0  # sell this many hours after the start (negative: before)

    @classmethod
    def from_settings(cls, settings: Settings) -> "Params":
        return cls(
            dip_pct=settings.dip_pct,
            min_margin_pct=settings.min_margin_pct,
            min_profit=settings.min_profit,
            threshold=settings.promo_prebuy_threshold,
        )

    def with_value(self, name: str, value: float) -> "Params":
        kind = {f.name: f.type for f in fields(self)}[name]
        changes: dict[str, Any] = {name: int(value) if kind in (int, "int") else float(value)}
        return replace(self, **changes)


SWEEPABLE: dict[sig.Rule, tuple[str, ...]] = {
    sig.Rule.BUY_DIP: ("dip_pct", "min_margin_pct", "max_hold_h", "stop_loss_pct"),
    sig.Rule.PROMO_PREBUY: ("threshold", "entry_days", "exit_h"),
}
SUPPORTED = tuple(SWEEPABLE)


@dataclass(frozen=True)
class Trade:
    ea_id: int
    name: str
    bought_at: datetime
    buy: int
    sold_at: datetime  # for open positions: time of the last price in the window
    sell: int  # sale price (open: market value)
    exit: Exit
    note: str = ""

    @property
    def open(self) -> bool:
        return self.exit is Exit.OPEN

    @property
    def profit(self) -> int:
        return net_after_tax(self.sell) - self.buy

    @property
    def return_pct(self) -> float:
        return self.profit / self.buy * 100

    @property
    def held_h(self) -> float:
        return (self.sold_at - self.bought_at).total_seconds() / 3600


def market_sale(price: int) -> int:
    """Quick sale: one step below the current lowest BIN."""
    return step_price(price, -1)


def _index_until(points: Sequence[Point], at: datetime) -> int:
    """Number of points at or before `at`."""
    return bisect_right([t for t, _ in points], at)


def quote(points: Sequence[Point], at: datetime, max_age: timedelta = MAX_QUOTE_AGE) -> int | None:
    """Newest price at or before `at`, if it is not older than `max_age`."""
    i = _index_until(points, at)
    if i == 0:
        return None
    when, price = points[i - 1]
    return price if at - when <= max_age else None


def stats_at(
    points: Sequence[Point], index: int, times: Sequence[datetime] | None = None
) -> PriceStats:
    """Price statistics as the analysis saw them at snapshot `index` (7-day window)."""
    now = points[index][0]
    times = times if times is not None else [t for t, _ in points]
    # One point before the window keeps the 24 h change identical to the full history.
    lo = max(0, bisect_left(times, now - WEEK) - 1)
    return price_stats(points[lo : index + 1], now)


# --- BUY_DIP -------------------------------------------------------------------------


StatsCache = dict[int, list[tuple[int, PriceStats]]]


def dip_stats(series: Iterable[CardSeries], start: datetime, end: datetime) -> StatsCache:
    """Statistics at every snapshot inside the window; independent of the parameters."""
    cache: StatsCache = {}
    for card in series:
        times = [t for t, _ in card.points]
        cache[card.ea_id] = [
            (i, stats_at(card.points, i, times))
            for i, (at, _) in enumerate(card.points)
            if start <= at <= end
        ]
    return cache


def _close(
    card: CardSeries, index: int, target: int | None, params: Params, end: datetime, note: str
) -> tuple[Trade, int]:
    """Follow a position opened at snapshot `index` until target, stop, holding period or end."""
    bought_at, buy = card.points[index]
    hold_until = bought_at + timedelta(hours=params.max_hold_h)
    stop = buy * (1 - params.stop_loss_pct / 100) if params.stop_loss_pct > 0 else None
    last = index
    for j in range(index + 1, len(card.points)):
        at, price = card.points[j]
        if at > end:
            break
        last = j

        def done(sell: int, exit_: Exit, j: int = j, at: datetime = at) -> tuple[Trade, int]:
            return Trade(card.ea_id, card.name, bought_at, buy, at, sell, exit_, note), j

        if target is not None and price >= target:
            return done(target, Exit.TARGET)
        if stop is not None and price <= stop:
            return done(market_sale(price), Exit.STOP)
        if at >= hold_until:
            return done(market_sale(price), Exit.HOLD)
    at, price = card.points[last]
    trade = Trade(card.ea_id, card.name, bought_at, buy, at, market_sale(price), Exit.OPEN, note)
    return trade, len(card.points)


def simulate_dip(
    series: Sequence[CardSeries],
    cache: StatsCache,
    params: Params,
    end: datetime,
) -> list[Trade]:
    """BUY_DIP: buy when the price is clearly below the 7-day mean, sell at the mean."""
    cfg = sig.SignalConfig(
        dip_pct=params.dip_pct, min_margin_pct=params.min_margin_pct, min_profit=params.min_profit
    )
    trades: list[Trade] = []
    for card in series:
        free_from = 0
        for index, stats in cache.get(card.ea_id, []):
            if index < free_from:
                continue
            signal = sig.buy_dip(card.ea_id, card.name, stats, cfg)
            if signal is None or stats.week is None:
                continue
            target = round_to_price_step(stats.week.mean, "down")
            note = f"{stats.deviation_pct or 0:+.1f} % unter Ø".replace(".", ",")
            trade, exit_index = _close(card, index, target, params, end, note)
            trades.append(trade)
            free_from = exit_index + 1
    return sorted(trades, key=lambda t: (t.bought_at, t.ea_id))


# --- PROMO_PREBUY -------------------------------------------------------------------------


def simulate_prebuy(
    series: Sequence[CardSeries],
    promos: Sequence[PromoInfo],
    params: Params,
    weights: PromoWeights,
    start: datetime,
    end: datetime,
) -> list[Trade]:
    """PROMO_PREBUY: buy `entry_days` before the start if the card scores, sell `exit_h` after."""
    weights = replace(weights, threshold=params.threshold)
    trades: list[Trade] = []
    for promo in sorted(promos, key=lambda p: (p.starts_at, p.promo_id)):
        decide_at = promo.starts_at - timedelta(days=params.entry_days)
        sell_at = promo.starts_at + timedelta(hours=params.exit_h)
        if not start <= decide_at <= end:
            continue
        for card in series:
            count = _index_until(card.points, decide_at)
            if count == 0 or quote(card.points, decide_at) is None:
                continue
            stats = stats_at(card.points, count - 1)
            match = score_card(card.info, promo, stats, decide_at, weights)
            if match is None or not is_prebuy(match, weights):
                continue
            bought_at, buy = card.points[count - 1]
            note = f"{promo.name} (Score {match.score:.0f})"
            trades.append(_sell_after(card, count, bought_at, buy, sell_at, end, note))
    return sorted(trades, key=lambda t: (t.bought_at, t.ea_id))


def _sell_after(
    card: CardSeries,
    start_index: int,
    bought_at: datetime,
    buy: int,
    sell_at: datetime,
    end: datetime,
    note: str,
) -> Trade:
    last: Point = (bought_at, buy)
    for at, price in card.points[start_index:]:
        if at > end:
            break
        if at >= sell_at:
            return Trade(
                card.ea_id, card.name, bought_at, buy, at, market_sale(price), Exit.EVENT, note
            )
        last = (at, price)
    at, price = last
    return Trade(card.ea_id, card.name, bought_at, buy, at, market_sale(price), Exit.OPEN, note)


# --- metrics ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Metrics:
    trades: int  # closed trades
    open: int
    wins: int
    total_profit: int
    unrealized: int  # open positions at market value
    max_drawdown: int  # largest drop of the cumulative profit, in coins
    peak_capital: int  # most coins tied up at the same time
    avg_hold_h: float | None

    @property
    def hit_rate(self) -> float | None:
        return self.wins / self.trades * 100 if self.trades else None

    @property
    def avg_profit(self) -> float | None:
        return self.total_profit / self.trades if self.trades else None


def metrics(trades: Sequence[Trade]) -> Metrics:
    closed = sorted((t for t in trades if not t.open), key=lambda t: (t.sold_at, t.ea_id))
    equity = peak = drawdown = 0
    for trade in closed:
        equity += trade.profit
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)

    # Capital: +buy when bought, -buy when sold (sales first at equal times); open stays tied up.
    events = sorted(
        [(t.bought_at, 1, t.buy) for t in trades] + [(t.sold_at, 0, -t.buy) for t in closed]
    )
    capital = peak_capital = 0
    for _, _, amount in events:
        capital += amount
        peak_capital = max(peak_capital, capital)

    return Metrics(
        trades=len(closed),
        open=len(trades) - len(closed),
        wins=sum(1 for t in closed if t.profit > 0),
        total_profit=sum(t.profit for t in closed),
        unrealized=sum(t.profit for t in trades if t.open),
        max_drawdown=drawdown,
        peak_capital=peak_capital,
        avg_hold_h=sum(t.held_h for t in closed) / len(closed) if closed else None,
    )


# --- parameter sweep ---------------------------------------------------------------------


@dataclass(frozen=True)
class SweepRow:
    value: float
    metrics: Metrics


def sweep(
    run: Callable[[Params], list[Trade]], params: Params, name: str, values: Sequence[float]
) -> list[SweepRow]:
    return [SweepRow(value, metrics(run(params.with_value(name, value)))) for value in values]
