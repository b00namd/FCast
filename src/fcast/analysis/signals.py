"""Trading signals: BUY_DIP, SELL_TARGET and OVERPRICE_CHANCE (ÜV). Pure functions.

Every signal carries a recommendation on a valid price step and the expected profit after
the 5 % EA tax, plus human-readable reasons (German, shown in the dashboard and alerts).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from fcast.analysis.market import effective_price, headroom_pct, supply_gap_pct
from fcast.analysis.pricing import (
    net_after_tax,
    profit,
    round_to_price_step,
    step_price,
)
from fcast.analysis.stats import PriceStats
from fcast.config import Settings

MIN_POINTS_DIP = 6  # a 7-day mean from fewer snapshots is not trustworthy
# New cards (e.g. a fresh TOTW) fall for days after release; a "mean" over the first hours is
# just the launch price, so a dip needs a few days of history.
MIN_SPAN_DIP = timedelta(days=3)
FULL_LISTINGS = 5  # FUTBIN shows at most five lowest BINs
THIN_MIN_HOURS = 6.0  # thin supply counts as persistent from this duration on


class Rule(StrEnum):
    BUY_DIP = "BUY_DIP"
    SELL_TARGET = "SELL_TARGET"
    OVERPRICE_CHANCE = "OVERPRICE_CHANCE"
    PROMO_PREBUY = "PROMO_PREBUY"
    HOLO_SPREAD = "HOLO_SPREAD"


RULE_LABELS = {
    Rule.BUY_DIP: "Kauf-Dip",
    Rule.SELL_TARGET: "Verkaufsziel",
    Rule.OVERPRICE_CHANCE: "ÜV-Chance",
    Rule.PROMO_PREBUY: "Promo-Vorkauf",
    Rule.HOLO_SPREAD: "Holo-ÜV",
}


@dataclass(frozen=True)
class Signal:
    rule: Rule
    ea_id: int
    name: str
    price: int | None  # current market price
    reference: int | None  # 7-day mean
    recommended: int | None  # max buy price (BUY_DIP) or listing price (sell signals)
    expected_profit: int | None
    score: float | None
    reasons: tuple[str, ...]

    @property
    def label(self) -> str:
        return RULE_LABELS[self.rule]


@dataclass(frozen=True)
class SignalConfig:
    dip_pct: float = 10.0
    min_margin_pct: float = 5.0
    min_profit: int = 500
    outlier_gap_pct: float = 15.0
    uev_threshold: float = 60.0
    uev_weight_supply: float = 0.40
    uev_weight_trend: float = 0.25
    uev_weight_headroom: float = 0.15
    uev_weight_liquidity: float = 0.20
    uev_min_markup_pct: float = 5.0
    uev_extinct_markup_pct: float = 20.0
    holo_min_spread_pct: float = 30.0
    holo_max_spread_pct: float = 150.0
    holo_min_history_h: float = 48.0

    @classmethod
    def from_settings(cls, settings: Settings) -> "SignalConfig":
        return cls(**{name: getattr(settings, name) for name in cls.__dataclass_fields__})


@dataclass(frozen=True)
class Supply:
    """Supply picture as the signals need it (decoupled from the ORM model)."""

    listings: tuple[int, ...]
    range_max: int | None = None
    extinct_since: datetime | None = None
    thin_hours: float | None = None  # how long supply has been thin without a break

    @property
    def extinct(self) -> bool:
        return not self.listings

    @classmethod
    def observed(
        cls,
        listings: tuple[int, ...],
        range_max: int | None,
        outlier_gap_pct: float,
        extinct_since: datetime | None = None,
        thin_hours: float | None = None,
    ) -> "Supply":
        """Supply from raw lowest BINs, without a single outlier listing far below the rest."""
        effective = effective_price(listings, outlier_gap_pct)
        kept = tuple(p for p in listings if effective is None or p >= effective)
        return cls(kept, range_max, extinct_since, thin_hours)


def thin_since(observations: Sequence[tuple[datetime, int]]) -> datetime | None:
    """Start of the current run of thin supply (fewer than five listings, or none).

    `observations` are (time, number of listings), oldest first. None if supply is full now.
    """
    start = None
    for at, count in reversed(observations):
        if count >= FULL_LISTINGS:
            break
        start = at
    return start


def _coins(value: float) -> str:
    return f"{round(value):,}".replace(",", ".")


def _pct(value: float) -> str:
    return f"{value:+.1f} %".replace(".", ",")


def _required_profit(buy: int, cfg: SignalConfig) -> float:
    return max(cfg.min_profit, buy * cfg.min_margin_pct / 100)


def buy_dip(ea_id: int, name: str, stats: PriceStats, cfg: SignalConfig) -> Signal | None:
    """Price clearly below the 7-day mean and selling at the mean pays off after tax."""
    week = stats.week
    if stats.current is None or week is None or week.count < MIN_POINTS_DIP:
        return None
    if week.span < MIN_SPAN_DIP:
        return None
    if stats.current > week.mean * (1 - cfg.dip_pct / 100):
        return None
    sell = round_to_price_step(week.mean, "down")
    expected = profit(stats.current, sell)
    if expected < _required_profit(stats.current, cfg):
        return None
    # Highest buy price that still leaves the required margin when selling at the mean.
    proceeds = net_after_tax(sell)
    max_buy = min(proceeds / (1 + cfg.min_margin_pct / 100), proceeds - cfg.min_profit)
    return Signal(
        rule=Rule.BUY_DIP,
        ea_id=ea_id,
        name=name,
        price=stats.current,
        reference=round(week.mean),
        recommended=round_to_price_step(max_buy, "down"),
        expected_profit=expected,
        score=None,
        reasons=(
            f"{_pct(stats.deviation_pct or 0)} gegenüber Ø 7 Tage ({_coins(week.mean)})",
            f"Verkauf zum Ø bringt {_coins(expected)} nach Steuer",
        ),
    )


def sell_target(ea_id: int, name: str, stats: PriceStats, target_sell: int | None) -> Signal | None:
    if stats.current is None or target_sell is None or stats.current < target_sell:
        return None
    return Signal(
        rule=Rule.SELL_TARGET,
        ea_id=ea_id,
        name=name,
        price=stats.current,
        reference=round(stats.week.mean) if stats.week else None,
        recommended=round_to_price_step(stats.current, "down"),
        expected_profit=None,
        score=None,
        reasons=(f"Preis {_coins(stats.current)} ≥ Verkaufsziel {_coins(target_sell)}",),
    )


@dataclass(frozen=True)
class OverpriceScore:
    score: float
    supply: float
    trend: float
    headroom: float
    liquidity: float
    reasons: tuple[str, ...]


def overprice_score(
    stats: PriceStats, supply: Supply | None, cfg: SignalConfig
) -> OverpriceScore | None:
    """ÜV score 0-100: thin supply, rising price, room below EA's maximum, active market."""
    if stats.current is None:
        return None
    reasons: list[str] = []

    # Supply: extinct is the extreme case; otherwise gaps and few listings.
    if supply is None:
        supply_part = 0.0
    elif supply.extinct:
        supply_part = 1.0
        reasons.append("Keine Angebote (extinct)")
    else:
        gap = supply_gap_pct(supply.listings)
        gap_part = min((gap or 0) / 30, 1.0)
        count = len(supply.listings)
        count_part = max(0.0, (FULL_LISTINGS - count) / (FULL_LISTINGS - 1)) * 0.8
        supply_part = max(gap_part, count_part)
        if count < FULL_LISTINGS:
            reasons.append(f"Nur {count} Angebot{'e' if count != 1 else ''}")
        if gap is not None and gap >= 5:
            reasons.append(f"Lücke zum nächsten Angebot {gap:.0f} %".replace(".", ","))
        # Thin for a long time: the scarcity is real, not a snapshot between two listings.
        if supply.thin_hours is not None and supply.thin_hours >= THIN_MIN_HOURS:
            supply_part = min(1.0, supply_part + 0.2 * min(supply.thin_hours / 48, 1.0))
            reasons.append(f"Angebot seit {supply.thin_hours:.0f} h dünn")

    # Trend: rising prices mean demand; use the 24 h change, else the deviation from the mean.
    change = stats.change_24h_pct if stats.change_24h_pct is not None else stats.deviation_pct
    trend_part = min(max((change or 0) / 10, 0.0), 1.0)
    if change is not None and change > 0:
        reasons.append(f"Trend {_pct(change)}")

    room = headroom_pct(stats.current, supply.range_max if supply else None)
    headroom_part = 0.5 if room is None else min(room / 50, 1.0)
    if room is not None and room < 10:
        reasons.append(f"Kaum Luft bis EA-Maximum ({room:.0f} %)".replace(".", ","))

    liquidity_part = min((stats.changes_per_day or 0) / 12, 1.0)

    weights = (
        cfg.uev_weight_supply,
        cfg.uev_weight_trend,
        cfg.uev_weight_headroom,
        cfg.uev_weight_liquidity,
    )
    parts = (supply_part, trend_part, headroom_part, liquidity_part)
    total = sum(weights) or 1.0
    score = 100 * sum(w * p for w, p in zip(weights, parts, strict=True)) / total
    return OverpriceScore(
        score=round(score, 1),
        supply=supply_part,
        trend=trend_part,
        headroom=headroom_part,
        liquidity=liquidity_part,
        reasons=tuple(reasons),
    )


def overprice_listing_price(
    stats: PriceStats, supply: Supply | None, cfg: SignalConfig
) -> int | None:
    """Suggested listing price for ÜV, on a valid step and below EA's maximum."""
    if stats.current is None:
        return None
    cap = supply.range_max if supply and supply.range_max else None
    if supply is not None and supply.extinct:
        target = round_to_price_step(stats.current * (1 + cfg.uev_extinct_markup_pct / 100), "down")
    else:
        minimum = round_to_price_step(stats.current * (1 + cfg.uev_min_markup_pct / 100), "up")
        above = [p for p in (supply.listings if supply else ()) if p > stats.current]
        just_below_next = step_price(above[0], -1) if above else 0
        target = max(minimum, just_below_next)
    if cap is not None:
        target = min(target, round_to_price_step(cap, "down"))
    return target


def overprice_chance(
    ea_id: int, name: str, stats: PriceStats, supply: Supply | None, cfg: SignalConfig
) -> Signal | None:
    result = overprice_score(stats, supply, cfg)
    if result is None or result.score < cfg.uev_threshold or stats.current is None:
        return None
    target = overprice_listing_price(stats, supply, cfg)
    if target is None or target <= stats.current:
        return None
    extinct = supply is not None and supply.extinct
    # Extinct cards cannot be bought, so the profit only applies to cards you already own.
    expected = None if extinct else profit(stats.current, target)
    # Same bar as BUY_DIP: buying now and listing at the target must be worth it.
    if expected is not None and expected < _required_profit(stats.current, cfg):
        return None
    return Signal(
        rule=Rule.OVERPRICE_CHANCE,
        ea_id=ea_id,
        name=name,
        price=stats.current,
        reference=round(stats.week.mean) if stats.week else None,
        recommended=target,
        expected_profit=expected,
        score=result.score,
        reasons=result.reasons,
    )


@dataclass(frozen=True)
class HoloQuote:
    """Price picture of the holo partner of a card."""

    price: int | None  # current lowest BIN, or the last known price if extinct
    extinct: bool
    stale: bool = False  # price is only the last known one


def holo_spread_pct(normal_price: int | None, holo: HoloQuote | None) -> float | None:
    if not normal_price or holo is None or not holo.price:
        return None
    return (holo.price - normal_price) / normal_price * 100


def holo_spread(
    ea_id: int,
    name: str,
    stats: PriceStats,
    holo: HoloQuote | None,
    supply: Supply | None,
    cfg: SignalConfig,
) -> Signal | None:
    """The holo trades far above the normal card: list the normal one just below the holo.

    Only when it is realistic: a moderate spread, the normal card is scarce (fewer than five
    listings) and there is enough price history (fresh cards still fall from launch prices).
    """
    spread = holo_spread_pct(stats.current, holo)
    if spread is None or holo is None or holo.price is None or stats.current is None:
        return None
    if not cfg.holo_min_spread_pct <= spread <= cfg.holo_max_spread_pct:
        return None
    if supply is None or supply.extinct or len(supply.listings) >= FULL_LISTINGS:
        return None
    if stats.week is None or stats.week.span < timedelta(hours=cfg.holo_min_history_h):
        return None
    target = step_price(round_to_price_step(holo.price, "down"), -1)
    if supply.range_max:
        target = min(target, round_to_price_step(supply.range_max, "down"))
    expected = profit(stats.current, target)
    if expected <= 0:
        return None
    reasons = [
        f"Holo {_coins(holo.price)} vs. normal {_coins(stats.current)} ({_pct(spread)})",
    ]
    if holo.extinct:
        reasons.append("Holo gerade extinct" + (" (letzter bekannter Preis)" if holo.stale else ""))
    reasons.append(f"normale Karte knapp ({len(supply.listings)} Angebote)")
    return Signal(
        rule=Rule.HOLO_SPREAD,
        ea_id=ea_id,
        name=name,
        price=stats.current,
        reference=holo.price,
        recommended=target,
        expected_profit=expected,
        score=round(min(spread, 100.0), 1),
        reasons=tuple(reasons),
    )
