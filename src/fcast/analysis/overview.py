"""Market overview for on-demand analysis (`fcast lage`): everything relevant in one structure.

Collects per-card figures (price, trend, range, supply, ÜV score, holo, cheapest time of day,
signals) plus market mood, upcoming promos and TOTW, recent alerts, source health and a
BUY_DIP backtest of the last 30 days. Read-only.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from fcast.analysis import signals as sig
from fcast.analysis.service import analyze_watchlist
from fcast.config import Settings
from fcast.db import repositories as repo
from fcast.db.models import TotwPrediction
from fcast.totw import calendar

MIN_DAYS_HOUR_PROFILE = 3  # a time-of-day pattern needs a few days of data
MIN_DAYS_WEEKDAY_PROFILE = 14
ALERT_WINDOW = timedelta(hours=24)
PROMO_WINDOW = timedelta(days=14)
RADAR_TOP = 15
WEEKDAYS = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")


@dataclass(frozen=True)
class CardLine:
    ea_id: int
    name: str
    card_type: str | None
    note: str | None
    price: int | None
    price_age_h: float | None
    change_24h_pct: float | None
    deviation_pct: float | None  # vs. 7-day mean
    week_low: int | None
    week_high: int | None
    points_7d: int
    days_of_data: int
    volatility_pct: float | None
    listings: tuple[int, ...]
    extinct: bool
    gap_pct: float | None
    headroom_pct: float | None
    thin_hours: float | None  # supply below five listings for this long
    uev_score: float | None
    holo_price: int | None
    holo_spread_pct: float | None
    cheapest_hour: int | None
    cheapest_weekday: str | None
    target_buy: int | None
    target_sell: int | None
    signals: tuple[str, ...]


@dataclass(frozen=True)
class Mood:
    cards: int
    avg_change_24h_pct: float | None
    rising: int
    falling: int


@dataclass(frozen=True)
class PromoLine:
    name: str
    starts_at: datetime
    confidence: float
    candidates: tuple[str, ...]  # "Name (score)"


@dataclass(frozen=True)
class TotwLine:
    rank: int
    name: str
    team: str
    reasons: str
    card: str | None
    price: int | None


@dataclass
class Overview:
    generated_at: datetime
    platform: str
    cards: list[CardLine]
    mood: Mood
    promos: list[PromoLine] = field(default_factory=list)
    totw_week: int | None = None
    totw_release: datetime | None = None
    totw: list[TotwLine] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    backtest: str | None = None
    radar: list[str] = field(default_factory=list)  # top radar hits
    fodder: list[str] = field(default_factory=list)  # fodder index per rating


def _days(points: list[datetime]) -> int:
    return len({p.date() for p in points})


def card_lines(session: Session, settings: Settings, now: datetime) -> list[CardLine]:
    lines = []
    for a in analyze_watchlist(session, settings, now):
        stats = a.stats
        snapshots = repo.list_snapshots(
            session, a.player, settings.platform, since=now - timedelta(days=30)
        )
        days = _days([s.captured_at for s in snapshots])
        hours, weekdays = a.hours, a.weekdays
        hour = (
            min(hours, key=lambda h: hours[h]) if hours and days >= MIN_DAYS_HOUR_PROFILE else None
        )
        weekday = (
            WEEKDAYS[min(weekdays, key=lambda d: weekdays[d])]
            if weekdays and days >= MIN_DAYS_WEEKDAY_PROFILE
            else None
        )
        lines.append(
            CardLine(
                ea_id=a.player.ea_id,
                name=a.player.display_name,
                card_type=a.player.card_type,
                note=a.entry.note if a.entry else None,
                price=stats.current,
                price_age_h=(
                    round((now - stats.current_at).total_seconds() / 3600, 1)
                    if stats.current_at
                    else None
                ),
                change_24h_pct=stats.change_24h_pct,
                deviation_pct=stats.deviation_pct,
                week_low=stats.week.low if stats.week else None,
                week_high=stats.week.high if stats.week else None,
                points_7d=stats.week.count if stats.week else 0,
                days_of_data=days,
                volatility_pct=stats.volatility_pct,
                listings=tuple(a.market.listings) if a.market else (),
                extinct=a.extinct,
                gap_pct=a.supply_gap_pct,
                headroom_pct=a.headroom_pct,
                thin_hours=a.supply.thin_hours if a.supply else None,
                uev_score=a.overprice.score if a.overprice else None,
                holo_price=a.holo.price if a.holo else None,
                holo_spread_pct=a.holo_spread_pct,
                cheapest_hour=hour,
                cheapest_weekday=weekday,
                target_buy=a.entry.target_buy if a.entry else None,
                target_sell=a.entry.target_sell if a.entry else None,
                signals=tuple(s.label for s in a.signals),
            )
        )
    return lines


def mood(cards: list[CardLine]) -> Mood:
    changes = [c.change_24h_pct for c in cards if c.change_24h_pct is not None]
    return Mood(
        cards=len(cards),
        avg_change_24h_pct=sum(changes) / len(changes) if changes else None,
        rising=sum(1 for c in changes if c > 1),
        falling=sum(1 for c in changes if c < -1),
    )


def build_overview(session: Session, settings: Settings, now: datetime) -> Overview:
    from fcast.backtest import service as bt
    from fcast.promos import service as promo_service

    cards = card_lines(session, settings, now)
    overview = Overview(
        generated_at=now, platform=settings.platform.value, cards=cards, mood=mood(cards)
    )

    candidates = promo_service.candidates(session, settings, now)
    for promo in repo.list_promos(session, starting_after=now - timedelta(days=3)):
        if promo.starts_at > now + PROMO_WINDOW:
            continue
        linked = [
            f"{c.player.display_name} ({c.match.score:.0f})"
            for c in candidates
            if c.match.promo.promo_id == promo.id
        ]
        overview.promos.append(
            PromoLine(promo.name, promo.starts_at, promo.confidence, tuple(linked[:10]))
        )

    release_time = calendar.parse_release_time(settings.totw_release_time)
    week = calendar.upcoming_week(now, settings.totw_first_release, release_time, settings.tz)
    overview.totw_week, overview.totw_release = week.number, week.release
    for p in session.scalars(
        select(TotwPrediction)
        .where(TotwPrediction.week == week.number)
        .order_by(TotwPrediction.rank)
        .limit(10)
    ):
        overview.totw.append(TotwLine(p.rank, p.name, p.team, p.reasons, p.card_name, p.price))

    overview.alerts = [
        f"{a.sent_at.astimezone(settings.tz):%d.%m. %H:%M} {a.rule}: "
        + a.message.replace("\n", " · ")[:160]
        for a in repo.list_alerts(session, limit=30)
        if a.sent_at >= now - ALERT_WINDOW
    ]
    for status in repo.list_source_statuses(session):
        line = f"{status.source}: ok"
        if status.paused_until is not None and status.paused_until > now:
            line = f"{status.source}: pausiert bis {status.paused_until:%d.%m. %H:%M} UTC"
        if status.last_success_at is not None:
            line += f", letzter Erfolg {status.last_success_at:%d.%m. %H:%M} UTC"
        overview.sources.append(line)

    from fcast.radar import service as radar

    for hit in radar.hits(session, settings, now)[:RADAR_TOP]:
        where = "Watchlist" if hit.on_watchlist else (hit.list_name or "-")
        overview.radar.append(
            f"{hit.player.display_name} [{where}] Potenzial {hit.potential:.0f}, "
            f"Preis {hit.price or '-'}: "
            + " | ".join(f"{s.label} {s.score:.0f}: {', '.join(s.reasons)}" for s in hit.signals)
        )
    overview.fodder = [
        f"{line.rating}er {line.price}"
        + (f" ({line.change_pct:+.1f} % in 24 h)" if line.change_pct is not None else "")
        + (f" - {line.signal.label}" if line.signal else "")
        for line in radar.fodder(session, settings, now)
    ]

    start = now - timedelta(days=30)
    report = bt.run_backtest(session, settings, sig.Rule.BUY_DIP, start, now)
    m = report.metrics
    overview.backtest = (
        f"Kauf-Dip 30 Tage: {m.trades} Trades ({m.open} offen), "
        + (f"Treffer {m.hit_rate:.0f} %, " if m.hit_rate is not None else "")
        + f"Profit {m.total_profit:+,} Coins".replace(",", ".")
    )
    return overview
