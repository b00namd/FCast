"""Potential radar: keeps the scanner's card universe, prices it slowly and finds early signals."""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from fcast.analysis.cards import (
    CardValue,
    card_values,
    expected_price,
    play_value_validated,
    price_fit,
)
from fcast.analysis.stats import Point
from fcast.collector.job import apply_player_info
from fcast.config import Settings
from fcast.db import repositories as repo
from fcast.db.models import FodderPrice, Player, RadarCard
from fcast.db.session import session_scope
from fcast.radar import signals as rs
from fcast.radar.futbin_lists import CHEAPEST, LATEST, POPULAR, parse_fodder, player_refs
from fcast.sources.base import SourceBlockedError, SourceError
from fcast.sources.futbin import FutbinSource
from fcast.totw import calendar
from fcast.totw.futbin_totw import totw_path

logger = logging.getLogger(__name__)

FUTBIN = "futbin"
UNIVERSE_KEY = "radar.universe_at"
FODDER_KEY = "radar.fodder_at"
FODDER_RATINGS = range(82, 91)
FODDER_CHEAPEST = 3  # the index is the mean of the three cheapest cards of a rating
FORGET_AFTER = timedelta(days=7)  # drop cards that have not been on a list for a week
RESOLVE_RETRY = timedelta(days=1)
HISTORY = timedelta(days=3)


def _due(session: Session, key: str, hours: float, now: datetime) -> bool:
    value = repo.get_app_settings(session, key).get(key)
    return value is None or now - datetime.fromisoformat(value) >= timedelta(hours=hours)


# --- universe ---------------------------------------------------------------------------------


def current_totw_number(settings: Settings, now: datetime) -> int | None:
    release_time = calendar.parse_release_time(settings.totw_release_time)
    week = calendar.last_released_week(now, settings.totw_first_release, release_time, settings.tz)
    return week.number if week is not None else None


async def refresh_universe(
    futbin: FutbinSource, factory: sessionmaker[Session], settings: Settings, now: datetime
) -> int:
    """Read the FUTBIN lists (if due) and update the radar universe; returns new cards."""
    with session_scope(factory) as session:
        if not _due(session, UNIVERSE_KEY, settings.radar_universe_h, now):
            return 0
    lists = [("popular", POPULAR), ("latest", LATEST)]
    number = current_totw_number(settings, now)
    if number is not None:
        lists.append(("totw", totw_path(number)))
    seen: dict[str, str] = {}
    for name, path in lists:
        try:
            refs = player_refs(await futbin.get_page(path), settings.radar_list_limit)
        except SourceBlockedError:
            raise
        except SourceError as exc:
            logger.warning("radar list %s failed: %s", path, exc)
            continue
        for ref in refs:
            seen.setdefault(ref, name)  # popular first; a card keeps its first list
    new = 0
    with session_scope(factory) as session:
        for ref, name in seen.items():
            card = session.get(RadarCard, ref)
            if card is None:
                session.add(
                    RadarCard(futbin_ref=ref, list_name=name, first_seen_at=now, last_seen_at=now)
                )
                new += 1
            else:
                card.last_seen_at = now
                card.list_name = name
        for card in session.scalars(
            select(RadarCard).where(RadarCard.last_seen_at < now - FORGET_AFTER)
        ):
            session.delete(card)
        repo.set_app_setting(session, UNIVERSE_KEY, now.isoformat())
    logger.info("radar universe: %d cards on the lists, %d new", len(seen), new)
    return new


async def resolve_pending(
    futbin: FutbinSource, factory: sessionmaker[Session], settings: Settings, now: datetime
) -> list[int]:
    """Look up EA ids of new radar cards (one FUTBIN request each, the page is then cached)."""
    with session_scope(factory) as session:
        todo = [
            card.futbin_ref
            for card in session.scalars(
                select(RadarCard)
                .where(RadarCard.player_id.is_(None))
                .order_by(RadarCard.first_seen_at, RadarCard.futbin_ref)
            )
            if card.resolved_at is None or now - card.resolved_at >= RESOLVE_RETRY
        ][: settings.radar_resolve_per_run]
    resolved = []
    for ref in todo:
        try:
            info = await futbin.resolve_card(ref)
        except SourceBlockedError:
            raise
        except SourceError as exc:
            logger.warning("radar card %s not resolved: %s", ref, exc)
            info = None
        with session_scope(factory) as session:
            card = session.get(RadarCard, ref)
            if card is None:
                continue
            card.resolved_at = now
            if info is not None:
                player = apply_player_info(session, info)
                repo.set_source_ref(session, player, FUTBIN, ref)
                card.player_id = player.id
                resolved.append(info.ea_id)
    return resolved


def due_cards(session: Session, settings: Settings, now: datetime) -> list[int]:
    """Radar cards to price in this run: never or long ago checked, not on the watchlist."""
    watched = {e.player_id for e in repo.list_watchlist(session, active_only=True)}
    cutoff = now - timedelta(hours=settings.radar_interval_h)
    cards = session.scalars(select(RadarCard).where(RadarCard.player_id.is_not(None))).all()
    due = [
        card
        for card in cards
        if card.player_id not in watched and (card.checked_at is None or card.checked_at <= cutoff)
    ]
    epoch = datetime.min.replace(tzinfo=now.tzinfo)
    due.sort(key=lambda c: (c.checked_at or epoch, c.futbin_ref))
    return [card.player.ea_id for card in due[: settings.radar_per_run] if card.player]


def mark_checked(session: Session, ea_ids: list[int], now: datetime) -> None:
    if not ea_ids:
        return
    for card in session.scalars(
        select(RadarCard)
        .join(Player, RadarCard.player_id == Player.id)
        .where(Player.ea_id.in_(ea_ids))
    ):
        card.checked_at = now


# --- fodder -------------------------------------------------------------------------------------


async def refresh_fodder(
    futbin: FutbinSource, factory: sessionmaker[Session], settings: Settings, now: datetime
) -> bool:
    """Store the fodder index (if due); True if new prices were stored."""
    with session_scope(factory) as session:
        if not _due(session, FODDER_KEY, settings.radar_fodder_h, now):
            return False
    try:
        prices = parse_fodder(await futbin.get_page(CHEAPEST), settings.platform)
    except SourceBlockedError:
        raise
    except SourceError as exc:
        logger.warning("fodder page failed: %s", exc)
        return False
    with session_scope(factory) as session:
        for rating in FODDER_RATINGS:
            cheapest = prices.get(rating, [])[:FODDER_CHEAPEST]
            if cheapest:
                session.add(
                    FodderPrice(
                        platform=settings.platform,
                        rating=rating,
                        price=round(sum(cheapest) / len(cheapest)),
                        observed_at=now,
                    )
                )
        repo.set_app_setting(session, FODDER_KEY, now.isoformat())
    return True


def fodder_history(session: Session, settings: Settings, now: datetime) -> dict[int, list[Point]]:
    history: dict[int, list[Point]] = {}
    for row in session.scalars(
        select(FodderPrice)
        .where(FodderPrice.platform == settings.platform, FodderPrice.observed_at >= now - HISTORY)
        .order_by(FodderPrice.observed_at, FodderPrice.id)
    ):
        history.setdefault(row.rating, []).append((row.observed_at, row.price))
    return history


# --- evaluation -------------------------------------------------------------------------------


@dataclass
class RadarHit:
    player: Player
    list_name: str | None  # None: watchlist card
    on_watchlist: bool
    price: int | None
    futbin_ref: str | None
    signals: list[rs.RadarSignal] = field(default_factory=list)
    value: CardValue | None = None  # play value, meta score

    @property
    def potential(self) -> float:
        return rs.potential(self.signals, self.value.meta if self.value else None)


@dataclass
class RadarStatus:
    universe: int
    resolved: int
    checked_recently: int
    universe_at: str | None
    fodder_at: str | None


def status(session: Session, settings: Settings, now: datetime) -> RadarStatus:
    cards = session.scalars(select(RadarCard)).all()
    recent = now - timedelta(hours=settings.radar_interval_h)
    stored = repo.get_app_settings(session, "radar.")
    return RadarStatus(
        universe=len(cards),
        resolved=sum(1 for c in cards if c.player_id is not None),
        checked_recently=sum(1 for c in cards if c.checked_at and c.checked_at >= recent),
        universe_at=stored.get(UNIVERSE_KEY),
        fodder_at=stored.get(FODDER_KEY),
    )


def hits(session: Session, settings: Settings, now: datetime) -> list[RadarHit]:
    """All radar and watchlist cards with at least one early signal, strongest first."""
    cfg = rs.RadarConfig.from_settings(settings)
    radar = {
        card.player_id: card
        for card in session.scalars(select(RadarCard).where(RadarCard.player_id.is_not(None)))
    }
    watched = {e.player_id: e.player for e in repo.list_watchlist(session, active_only=True)}
    player_ids = set(radar) | set(watched)
    players = session.scalars(select(Player).where(Player.id.in_(player_ids))).all()

    usage: dict[int, tuple[float | None, float | None]] = {}
    for player in players:
        observed = [
            (u.observed_at, u.games) for u in repo.list_usage(session, player, now - HISTORY)
        ]
        usage[player.id] = rs.usage_rates(observed, now, cfg.usage_window_h)
    median = rs.cohort_median([current for current, _ in usage.values()])
    values = card_values(session, settings, now)
    # "Undervalued" rests on the play value: only once it is validated against real usage.
    fit = price_fit(values) if play_value_validated(values) else None

    found = []
    for player in players:
        points = [
            (s.captured_at, s.price)
            for s in repo.list_snapshots(session, player, settings.platform, since=now - HISTORY)
        ]
        observations = [
            (o.observed_at, len(o.listings))
            for o in repo.list_market_observations(
                session, player, settings.platform, since=now - HISTORY
            )
        ]
        current, previous = usage[player.id]
        value = values.get(player.id)
        expected = expected_price(fit, value) if value is not None else None
        signals = [
            s
            for s in (
                rs.trend_start(points, now, cfg),
                rs.supply_shrinking(observations, points, now, cfg),
                rs.usage_surge(current, previous, median, cfg),
                rs.undervalued(
                    value.meta if value else None,
                    value.price if value else None,
                    expected,
                    cfg,
                ),
            )
            if s is not None
        ]
        if not signals:
            continue
        card = radar.get(player.id)
        found.append(
            RadarHit(
                player=player,
                list_name=card.list_name if card else None,
                on_watchlist=player.id in watched,
                price=points[-1][1] if points else None,
                futbin_ref=repo.get_source_ref(session, player.ea_id, FUTBIN),
                signals=sorted(signals, key=lambda s: -s.score),
                value=value,
            )
        )
    return sorted(found, key=lambda h: (-h.potential, h.player.display_name))


def fodder(session: Session, settings: Settings, now: datetime) -> list[rs.FodderLine]:
    return rs.fodder_lines(
        fodder_history(session, settings, now), now, rs.RadarConfig.from_settings(settings)
    )
