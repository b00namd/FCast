"""Runs the analysis for cards from the database (used by dashboard, CLI and alerts)."""

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from fcast.analysis import signals as sig
from fcast.analysis.market import effective_price, headroom_pct, supply_gap_pct
from fcast.analysis.playvalue import PlayValue, play_value
from fcast.analysis.stats import Point, PriceStats, hour_profile, price_stats, weekday_profile
from fcast.config import Settings
from fcast.db import repositories as repo
from fcast.db.models import MarketState, Player, WatchlistEntry
from fcast.holo import holo_partner

PROFILE_WINDOW = timedelta(days=30)
WEAK_CARD = 40.0  # play value below which a dip gets a warning


@dataclass
class CardAnalysis:
    player: Player
    entry: WatchlistEntry | None
    stats: PriceStats
    market: MarketState | None
    supply: sig.Supply | None
    overprice: sig.OverpriceScore | None
    effective_price: int | None = None  # market price after outlier dampening
    thin_from: datetime | None = None  # supply thin (< 5 listings) since
    play: PlayValue | None = None  # how good the card is in the game
    holo_player: Player | None = None
    holo: sig.HoloQuote | None = None
    signals: list[sig.Signal] = field(default_factory=list)
    hours: dict[int, float] = field(default_factory=dict)
    weekdays: dict[int, float] = field(default_factory=dict)

    @property
    def extinct(self) -> bool:
        return self.supply is not None and self.supply.extinct

    @property
    def outlier(self) -> int | None:
        """The cheapest listing if it was ignored as outlier."""
        if self.market is None or not self.market.listings:
            return None
        cheapest = self.market.listings[0]
        return cheapest if self.effective_price != cheapest else None

    @property
    def holo_spread_pct(self) -> float | None:
        return sig.holo_spread_pct(self.stats.current, self.holo)

    @property
    def supply_gap_pct(self) -> float | None:
        return supply_gap_pct(self.market.listings) if self.market else None

    @property
    def headroom_pct(self) -> float | None:
        if self.stats.current is None or self.market is None:
            return None
        return headroom_pct(self.stats.current, self.market.range_max)


def _supply(
    market: MarketState | None,
    latest_at: datetime | None,
    outlier_gap_pct: float,
    thin_from: datetime | None = None,
) -> sig.Supply | None:
    """Supply for the signals: without outlier listings, and only if not older than the price."""
    if market is None:
        return None
    if latest_at is not None and market.observed_at < latest_at and market.listings:
        # A newer price came from a source without supply data; do not mix stale depth in.
        return None
    thin_hours = (
        (market.observed_at - thin_from).total_seconds() / 3600 if thin_from is not None else None
    )
    return sig.Supply.observed(
        market.listings, market.range_max, outlier_gap_pct, market.extinct_since, thin_hours
    )


def thin_supply_since(
    session: Session, player: Player, settings: Settings, now: datetime
) -> datetime | None:
    """Since when the card has had fewer than five listings without a break (last 7 days)."""
    observations = repo.list_market_observations(
        session, player, settings.platform, since=now - timedelta(days=7)
    )
    return sig.thin_since([(o.observed_at, len(o.listings)) for o in observations])


def _holo_quote(session: Session, holo: Player, settings: Settings) -> sig.HoloQuote | None:
    market = repo.get_market_state(session, holo, settings.platform)
    last = repo.latest_snapshot(session, holo, settings.platform)
    extinct = market is not None and market.extinct
    if last is None:
        return sig.HoloQuote(price=None, extinct=extinct)
    stale = extinct and market is not None and market.observed_at > last.captured_at
    return sig.HoloQuote(price=last.price, extinct=extinct, stale=stale)


def analyze_player(
    session: Session, player: Player, settings: Settings, now: datetime
) -> CardAnalysis:
    platform = settings.platform
    snapshots = repo.list_snapshots(session, player, platform, since=now - PROFILE_WINDOW)
    points: list[Point] = [(s.captured_at, s.price) for s in snapshots]
    stats = price_stats(points, now)
    market = repo.get_market_state(session, player, platform)
    thin_from = thin_supply_since(session, player, settings, now) if market else None
    supply = _supply(market, stats.current_at, settings.outlier_gap_pct, thin_from)
    entry = repo.get_watch(session, player)
    cfg = sig.SignalConfig.from_settings(settings)
    name = player.display_name

    holo_player = holo_partner(session, player)
    holo = _holo_quote(session, holo_player, settings) if holo_player is not None else None
    play = play_value(player.attributes, player.position)
    play_score = play.score if play is not None else None
    dip = sig.buy_dip(player.ea_id, name, stats, cfg)
    if dip is not None and play_score is not None and play_score < WEAK_CARD:
        # A weak card may not bounce back: say so instead of hiding the dip.
        dip = replace(
            dip,
            reasons=(*dip.reasons, f"Achtung: spielerisch schwach (Spielwert {play_score:.0f})"),
        )
    found = [
        dip,
        sig.sell_target(player.ea_id, name, stats, entry.target_sell if entry else None),
        sig.overprice_chance(player.ea_id, name, stats, supply, cfg, play_score),
        sig.holo_spread(player.ea_id, name, stats, holo, supply, cfg),
    ]
    return CardAnalysis(
        player=player,
        entry=entry,
        stats=stats,
        market=market,
        supply=supply,
        overprice=sig.overprice_score(stats, supply, cfg, play_score),
        thin_from=thin_from,
        play=play,
        holo_player=holo_player,
        holo=holo,
        effective_price=(
            effective_price(market.listings, settings.outlier_gap_pct) if market else None
        ),
        signals=[s for s in found if s is not None],
        hours=hour_profile(points, settings.tz),
        weekdays=weekday_profile(points, settings.tz),
    )


def analyze_watchlist(session: Session, settings: Settings, now: datetime) -> list[CardAnalysis]:
    return [
        analyze_player(session, entry.player, settings, now)
        for entry in repo.list_watchlist(session, active_only=True)
    ]


def current_signals(
    session: Session, settings: Settings, now: datetime, rule: sig.Rule | None = None
) -> list[sig.Signal]:
    """All signals for active watchlist cards, strongest first."""
    from fcast.promos.service import prebuy_signals  # promos build on the analysis

    found = [
        signal
        for analysis in analyze_watchlist(session, settings, now)
        for signal in analysis.signals
        if rule is None or signal.rule == rule
    ]
    if rule in (None, sig.Rule.PROMO_PREBUY):
        found += prebuy_signals(session, settings, now)
    order = {
        sig.Rule.BUY_DIP: 0,
        sig.Rule.PROMO_PREBUY: 1,
        sig.Rule.HOLO_SPREAD: 2,
        sig.Rule.OVERPRICE_CHANCE: 3,
        sig.Rule.SELL_TARGET: 4,
    }
    return sorted(found, key=lambda s: (order[s.rule], -(s.score or 0), s.name))
