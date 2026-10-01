"""Backtest runs and event curves on the database (used by the CLI and the dashboard)."""

import hashlib
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta, tzinfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from fcast.analysis import signals as sig
from fcast.analysis.stats import DAY, WEEK, Point
from fcast.backtest import engine
from fcast.backtest.curves import CardCurve, EventCurves, event_curve
from fcast.config import Settings
from fcast.db import repositories as repo
from fcast.db.models import (
    MarketObservation,
    Player,
    PriceSnapshot,
    SourceRef,
    TotwActual,
    TotwPrediction,
)
from fcast.promos.scoring import PromoInfo, PromoWeights, link_strength
from fcast.promos.service import FUTBIN
from fcast.totw import calendar
from fcast.totw.names import matches_slug

LOOKBACK = WEEK + DAY  # history the statistics at the window start need
EVENT_TAIL = timedelta(days=3)  # price data after an event that the curves use


def load_series(
    session: Session,
    settings: Settings,
    start: datetime,
    end: datetime,
    ea_ids: Sequence[int] | None = None,
) -> list[engine.CardSeries]:
    """Price series of all cards with snapshots in [start - lookback, end], by EA id."""
    stmt = (
        select(Player.id, PriceSnapshot.captured_at, PriceSnapshot.price)
        .join(PriceSnapshot, PriceSnapshot.player_id == Player.id)
        .where(
            PriceSnapshot.platform == settings.platform,
            PriceSnapshot.captured_at >= start - LOOKBACK,
            PriceSnapshot.captured_at <= end,
        )
        .order_by(Player.id, PriceSnapshot.captured_at, PriceSnapshot.id)
    )
    if ea_ids:
        stmt = stmt.where(Player.ea_id.in_(ea_ids))
    points: dict[int, list[Point]] = defaultdict(list)
    for player_id, at, price in session.execute(stmt):
        points[player_id].append((at, price))
    if not points:
        return []
    observations: dict[int, list[engine.Observation]] = defaultdict(list)
    for obs in session.scalars(
        select(MarketObservation)
        .where(
            MarketObservation.player_id.in_(points),
            MarketObservation.platform == settings.platform,
            MarketObservation.observed_at >= start - LOOKBACK,
            MarketObservation.observed_at <= end,
        )
        .order_by(MarketObservation.player_id, MarketObservation.observed_at, MarketObservation.id)
    ):
        observations[obs.player_id].append((obs.observed_at, obs.listings, obs.range_max))
    players = session.scalars(select(Player).where(Player.id.in_(points))).all()
    slugs = {
        ref.player_id: ref.external_ref.rsplit("/", 1)[-1]
        for ref in session.scalars(
            select(SourceRef).where(SourceRef.source == FUTBIN, SourceRef.player_id.in_(points))
        )
    }
    series = [
        engine.CardSeries(
            ea_id=player.ea_id,
            name=player.display_name,
            points=tuple(points[player.id]),
            slug=slugs.get(player.id),
            club=player.club,
            league=player.league,
            nation=player.nation,
            observations=tuple(observations[player.id]),
        )
        for player in players
    ]
    return sorted(series, key=lambda s: s.ea_id)


def promos_between(session: Session, start: datetime, end: datetime) -> list[PromoInfo]:
    return [
        PromoInfo(
            promo_id=promo.id,
            name=promo.name,
            starts_at=promo.starts_at,
            ends_at=promo.ends_at,
            confidence=promo.confidence,
            links=tuple((link.link_type.value, link.link_value) for link in promo.links),
        )
        for promo in repo.list_promos(session)
        if start <= promo.starts_at <= end
    ]


def fingerprint(
    rule: sig.Rule,
    start: datetime,
    end: datetime,
    params: engine.Params,
    series: Sequence[engine.CardSeries],
    promos: Sequence[PromoInfo] = (),
) -> str:
    """Short hash of everything the result depends on: same hash, same result."""
    digest = hashlib.sha256(f"{rule}|{start.isoformat()}|{end.isoformat()}|{params}".encode())
    for card in series:
        digest.update(f"{card.ea_id}:{card.club}:{card.league}:{card.nation}:{card.slug}".encode())
        for at, price in card.points:
            digest.update(f"{at.isoformat()}={price};".encode())
        for at, listings, range_max in card.observations:
            digest.update(f"{at.isoformat()}:{listings}:{range_max};".encode())
    for promo in promos:
        digest.update(repr(promo).encode())
    return digest.hexdigest()[:12]


_STATS_CACHE: dict[str, engine.StatsCache] = {}
_STATS_CACHE_SIZE = 4


def _dip_stats(
    series: Sequence[engine.CardSeries], start: datetime, end: datetime
) -> engine.StatsCache:
    """Statistics are the expensive part; reuse them while the data has not changed."""
    key = fingerprint(sig.Rule.BUY_DIP, start, end, engine.Params(), series)
    if key not in _STATS_CACHE:
        while len(_STATS_CACHE) >= _STATS_CACHE_SIZE:
            _STATS_CACHE.pop(next(iter(_STATS_CACHE)))
        _STATS_CACHE[key] = engine.dip_stats(series, start, end)
    return _STATS_CACHE[key]


@dataclass
class BacktestReport:
    rule: sig.Rule
    start: datetime
    end: datetime
    params: engine.Params
    trades: list[engine.Trade]
    metrics: engine.Metrics
    cards: int
    snapshots: int
    fingerprint: str
    promos: int = 0
    sweep_name: str | None = None
    sweep: list[engine.SweepRow] = field(default_factory=list)

    @property
    def best_sweep(self) -> engine.SweepRow | None:
        rows = [r for r in self.sweep if r.metrics.trades]
        return max(rows, key=lambda r: (r.metrics.total_profit, -r.value)) if rows else None


def run_backtest(
    session: Session,
    settings: Settings,
    rule: sig.Rule,
    start: datetime,
    end: datetime,
    params: engine.Params | None = None,
    sweep_name: str | None = None,
    sweep_values: Sequence[float] = (),
    ea_ids: Sequence[int] | None = None,
) -> BacktestReport:
    if rule not in engine.SUPPORTED:
        raise ValueError(f"Regel {rule} lässt sich nicht backtesten")
    if sweep_name is not None and sweep_name not in engine.SWEEPABLE[rule]:
        raise ValueError(f"Parameter {sweep_name} passt nicht zu {rule}")
    params = params or engine.Params.from_settings(settings)
    series = load_series(session, settings, start, end, ea_ids)
    promos: list[PromoInfo] = []

    if rule is sig.Rule.BUY_DIP:
        cache = _dip_stats(series, start, end)

        def run(p: engine.Params) -> list[engine.Trade]:
            return engine.simulate_dip(series, cache, p, end)

    elif rule is sig.Rule.TREND_START:

        def run(p: engine.Params) -> list[engine.Trade]:
            return engine.simulate_trend(series, p, start, end)

    elif rule is sig.Rule.OVERPRICE_CHANCE:
        cache = _dip_stats(series, start, end)  # same statistics, only the rule differs

        def run(p: engine.Params) -> list[engine.Trade]:
            return engine.simulate_uev(series, cache, p, end)

    else:
        # Decisions happen up to 7 days before a promo starts.
        promos = promos_between(session, start, end + timedelta(days=7))
        weights = PromoWeights.from_settings(settings)

        def run(p: engine.Params) -> list[engine.Trade]:
            return engine.simulate_prebuy(series, promos, p, weights, start, end)

    trades = run(params)
    rows = engine.sweep(run, params, sweep_name, sweep_values) if sweep_name else []
    return BacktestReport(
        rule=rule,
        start=start,
        end=end,
        params=params,
        trades=trades,
        metrics=engine.metrics(trades),
        cards=len(series),
        snapshots=sum(len(s.points) for s in series),
        fingerprint=fingerprint(rule, start, end, params, series, promos),
        promos=len(promos),
        sweep_name=sweep_name,
        sweep=rows,
    )


# --- event curves -------------------------------------------------------------------------


def _curves(series: Sequence[engine.CardSeries], event: datetime) -> list[CardCurve]:
    found = []
    for card in series:
        curve = event_curve(card.points, event)
        if curve is not None:
            found.append(CardCurve(card.ea_id, card.name, curve))
    return found


def promo_curves(
    session: Session, settings: Settings, start: datetime, end: datetime
) -> list[EventCurves]:
    """Price reaction of all tracked cards linked to a promo, around its start."""
    promos = promos_between(session, start, end)
    if not promos:
        return []
    series = load_series(session, settings, start, end + EVENT_TAIL)
    weights = PromoWeights.from_settings(settings)
    result = []
    for promo in sorted(promos, key=lambda p: p.starts_at):
        linked = [s for s in series if link_strength(s.info, promo, weights)[0] > 0]
        result.append(
            EventCurves(
                "promo", promo.name, promo.starts_at, "verknüpft", _curves(linked, promo.starts_at)
            )
        )
    return result


def totw_curves(
    session: Session, settings: Settings, start: datetime, end: datetime
) -> list[EventCurves]:
    """Price reaction of the predicted TOTW candidates' cards around the release."""
    release_time = calendar.parse_release_time(settings.totw_release_time)
    weeks = sorted(set(session.scalars(select(TotwPrediction.week))))
    releases = {
        n: calendar.release_at(settings.totw_first_release, release_time, settings.tz, n)
        for n in weeks
    }
    weeks = [n for n in weeks if start <= releases[n] <= end]
    if not weeks:
        return []
    series = {s.ea_id: s for s in load_series(session, settings, start, end + EVENT_TAIL)}
    result = []
    for number in weeks:
        predicted = session.scalars(
            select(TotwPrediction)
            .where(TotwPrediction.week == number, TotwPrediction.ea_id.is_not(None))
            .order_by(TotwPrediction.rank)
        ).all()
        actual = session.scalars(select(TotwActual.slug).where(TotwActual.week == number)).all()
        groups: dict[str, list[engine.CardSeries]] = defaultdict(list)
        for p in predicted:
            card = series.get(p.ea_id or 0)
            if card is None:
                continue
            if not actual:
                group = "Kandidaten"
            elif any(matches_slug(p.name, slug) for slug in actual):
                group = "im TOTW"
            else:
                group = "nicht im TOTW"
            groups[group].append(card)
        at = releases[number]
        for group, cards in sorted(groups.items()):
            result.append(EventCurves("totw", f"TOTW {number}", at, group, _curves(cards, at)))
    return result


# --- input helpers (CLI and dashboard) -----------------------------------------------------

DEFAULT_DAYS = 30


def window(
    first: date | None, last: date | None, tz: tzinfo, now: datetime
) -> tuple[datetime, datetime]:
    """Local calendar days [first, last] as UTC; default: the last 30 days up to now."""
    last = last or now.astimezone(tz).date()
    first = first or last - timedelta(days=DEFAULT_DAYS)
    if first > last:
        raise ValueError("Der Beginn liegt nach dem Ende")
    start = datetime.combine(first, time(0, 0), tzinfo=tz).astimezone(UTC)
    end = datetime.combine(last + timedelta(days=1), time(0, 0), tzinfo=tz).astimezone(UTC)
    return start, min(end - timedelta(microseconds=1), now)


def parse_sweep(rule: sig.Rule, text: str) -> tuple[str, list[float]]:
    """ "dip_pct=5,10,15" -> ("dip_pct", [5.0, 10.0, 15.0])."""
    name, _, values = text.partition("=")
    name = name.strip()
    allowed = engine.SWEEPABLE.get(rule, ())
    if name not in allowed:
        raise ValueError(
            f"Parameter „{name}“ gibt es für {rule} nicht (möglich: {', '.join(allowed)})"
        )
    try:
        numbers = [float(v) for v in values.replace(";", ",").replace(" ", ",").split(",") if v]
    except ValueError as exc:
        raise ValueError(f"Werte „{values}“ sind keine Zahlen") from exc
    if not numbers:
        raise ValueError("Keine Werte für den Sweep angegeben")
    return name, numbers
