"""CRUD helpers on top of an SQLAlchemy session.

Functions never commit; the caller owns the transaction (see `session_scope`).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from fcast.config import Platform
from fcast.db.base import utcnow
from fcast.db.models import (
    AlertLog,
    AppSetting,
    LinkType,
    MarketObservation,
    MarketState,
    Player,
    PortfolioListing,
    PortfolioPosition,
    PositionStatus,
    PriceSnapshot,
    Promo,
    PromoLink,
    SourceRef,
    SourceStatus,
    UsageObservation,
    WatchlistEntry,
)

# Collector runs drift by a few minutes (jitter); a card due "in 2 minutes" is collected now.
DUE_TOLERANCE = timedelta(minutes=5)


class NotFoundError(LookupError):
    pass


class InvalidStateError(ValueError):
    pass


def _require_positive(name: str, value: int | None) -> None:
    if value is not None and value <= 0:
        raise ValueError(f"{name} must be positive, got {value}")


# --- players ---------------------------------------------------------------


@dataclass(frozen=True)
class PlayerDetails:
    """Optional player attributes; `None` fields leave the stored value unchanged."""

    name: str | None = None
    rating: int | None = None
    position: str | None = None
    card_type: str | None = None
    league: str | None = None
    nation: str | None = None
    club: str | None = None
    chem_style: str | None = None
    chem_styles_raw: str | None = None
    games_used: int | None = None
    goals_per_game: float | None = None
    attributes_raw: str | None = None


def get_player_by_ea_id(session: Session, ea_id: int) -> Player | None:
    return session.scalar(select(Player).where(Player.ea_id == ea_id))


def get_player(session: Session, player_id: int) -> Player:
    player = session.get(Player, player_id)
    if player is None:
        raise NotFoundError(f"player {player_id} not found")
    return player


def upsert_player(session: Session, ea_id: int, details: PlayerDetails | None = None) -> Player:
    """Return the player for `ea_id`, creating it if needed and applying non-empty details."""
    _require_positive("ea_id", ea_id)
    player = get_player_by_ea_id(session, ea_id)
    if player is None:
        player = Player(ea_id=ea_id)
        session.add(player)
    if details is not None:
        for field, value in vars(details).items():
            if value is not None:
                setattr(player, field, value)
    session.flush()
    return player


def set_source_ref(session: Session, player: Player, source: str, external_ref: str) -> SourceRef:
    ref = session.get(SourceRef, (player.id, source))
    if ref is None:
        ref = SourceRef(player_id=player.id, source=source, external_ref=external_ref)
        session.add(ref)
    else:
        ref.external_ref = external_ref
    session.flush()
    return ref


def remove_source_ref(session: Session, player: Player, source: str) -> None:
    ref = session.get(SourceRef, (player.id, source))
    if ref is not None:
        session.delete(ref)
        session.flush()


def get_source_ref(session: Session, ea_id: int, source: str) -> str | None:
    return session.scalar(
        select(SourceRef.external_ref)
        .join(Player, Player.id == SourceRef.player_id)
        .where(Player.ea_id == ea_id, SourceRef.source == source)
    )


def list_source_refs(session: Session, player: Player) -> dict[str, str]:
    refs = session.scalars(select(SourceRef).where(SourceRef.player_id == player.id))
    return {ref.source: ref.external_ref for ref in refs}


# --- price snapshots -------------------------------------------------------


def add_snapshot(
    session: Session,
    player: Player,
    platform: Platform,
    price: int,
    source: str,
    captured_at: datetime | None = None,
) -> PriceSnapshot:
    _require_positive("price", price)
    snapshot = PriceSnapshot(
        player_id=player.id,
        platform=platform,
        price=price,
        source=source,
        captured_at=captured_at or utcnow(),
    )
    session.add(snapshot)
    session.flush()
    return snapshot


def list_snapshots(
    session: Session,
    player: Player,
    platform: Platform,
    since: datetime | None = None,
    until: datetime | None = None,
) -> Sequence[PriceSnapshot]:
    """Snapshots for one player and platform, oldest first."""
    stmt = select(PriceSnapshot).where(
        PriceSnapshot.player_id == player.id, PriceSnapshot.platform == platform
    )
    if since is not None:
        stmt = stmt.where(PriceSnapshot.captured_at >= since)
    if until is not None:
        stmt = stmt.where(PriceSnapshot.captured_at <= until)
    return session.scalars(stmt.order_by(PriceSnapshot.captured_at, PriceSnapshot.id)).all()


def latest_snapshot(session: Session, player: Player, platform: Platform) -> PriceSnapshot | None:
    return session.scalar(
        select(PriceSnapshot)
        .where(PriceSnapshot.player_id == player.id, PriceSnapshot.platform == platform)
        .order_by(PriceSnapshot.captured_at.desc(), PriceSnapshot.id.desc())
        .limit(1)
    )


def snapshot_before(
    session: Session, player: Player, platform: Platform, at: datetime
) -> PriceSnapshot | None:
    """Newest snapshot captured at or before `at` (e.g. the price 24 hours ago)."""
    return session.scalar(
        select(PriceSnapshot)
        .where(
            PriceSnapshot.player_id == player.id,
            PriceSnapshot.platform == platform,
            PriceSnapshot.captured_at <= at,
        )
        .order_by(PriceSnapshot.captured_at.desc(), PriceSnapshot.id.desc())
        .limit(1)
    )


def snapshot_exists(
    session: Session, player: Player, platform: Platform, source: str, captured_at: datetime
) -> bool:
    """True if this exact quote (same source and timestamp) was stored before."""
    return (
        session.scalar(
            select(PriceSnapshot.id)
            .where(
                PriceSnapshot.player_id == player.id,
                PriceSnapshot.platform == platform,
                PriceSnapshot.source == source,
                PriceSnapshot.captured_at == captured_at,
            )
            .limit(1)
        )
        is not None
    )


def record_market_state(
    session: Session,
    player: Player,
    platform: Platform,
    source: str,
    observed_at: datetime,
    listings: Sequence[int],
    range_min: int | None = None,
    range_max: int | None = None,
) -> MarketState:
    """Upsert the latest supply picture; tracks since when a card has been extinct."""
    state = session.get(MarketState, (player.id, platform))
    if state is None:
        state = MarketState(player_id=player.id, platform=platform)
        session.add(state)
    state.source = source
    state.observed_at = observed_at
    state.listings_csv = ",".join(str(value) for value in sorted(listings))
    state.range_min = range_min
    state.range_max = range_max
    if listings:
        state.extinct_since = None
    elif state.extinct_since is None:
        state.extinct_since = observed_at
    _record_observation(session, state, range_max)
    session.flush()
    return state


def _record_observation(session: Session, state: MarketState, range_max: int | None) -> None:
    """Append to the supply history (once per source and observation time)."""
    known = session.scalar(
        select(MarketObservation.id)
        .where(
            MarketObservation.player_id == state.player_id,
            MarketObservation.platform == state.platform,
            MarketObservation.source == state.source,
            MarketObservation.observed_at == state.observed_at,
        )
        .limit(1)
    )
    if known is None:
        session.add(
            MarketObservation(
                player_id=state.player_id,
                platform=state.platform,
                source=state.source,
                observed_at=state.observed_at,
                listings_csv=state.listings_csv,
                range_max=range_max,
            )
        )


def record_usage(session: Session, player: Player, games: int, observed_at: datetime) -> bool:
    """Append FUTBIN's games counter if it changed since the last observation."""
    last = session.scalar(
        select(UsageObservation)
        .where(UsageObservation.player_id == player.id)
        .order_by(UsageObservation.observed_at.desc(), UsageObservation.id.desc())
        .limit(1)
    )
    if last is not None and last.games == games:
        return False
    session.add(UsageObservation(player_id=player.id, observed_at=observed_at, games=games))
    session.flush()
    return True


def list_usage(
    session: Session, player: Player, since: datetime | None = None
) -> Sequence[UsageObservation]:
    stmt = select(UsageObservation).where(UsageObservation.player_id == player.id)
    if since is not None:
        stmt = stmt.where(UsageObservation.observed_at >= since)
    return session.scalars(stmt.order_by(UsageObservation.observed_at, UsageObservation.id)).all()


def list_market_observations(
    session: Session,
    player: Player,
    platform: Platform,
    since: datetime | None = None,
    until: datetime | None = None,
) -> Sequence[MarketObservation]:
    """Supply history of a card, oldest first."""
    stmt = select(MarketObservation).where(
        MarketObservation.player_id == player.id, MarketObservation.platform == platform
    )
    if since is not None:
        stmt = stmt.where(MarketObservation.observed_at >= since)
    if until is not None:
        stmt = stmt.where(MarketObservation.observed_at <= until)
    return session.scalars(stmt.order_by(MarketObservation.observed_at, MarketObservation.id)).all()


def get_market_state(session: Session, player: Player, platform: Platform) -> MarketState | None:
    return session.get(MarketState, (player.id, platform))


# --- watchlist -------------------------------------------------------------


def set_watch(
    session: Session,
    player: Player,
    target_buy: int | None = None,
    target_sell: int | None = None,
    note: str | None = None,
) -> WatchlistEntry:
    """Add a player to the watchlist or update an existing entry (reactivating it)."""
    _require_positive("target_buy", target_buy)
    _require_positive("target_sell", target_sell)
    entry = session.get(WatchlistEntry, player.id)
    if entry is None:
        entry = WatchlistEntry(player=player)
        session.add(entry)
    entry.target_buy = target_buy
    entry.target_sell = target_sell
    entry.note = note
    entry.active = True
    session.flush()
    return entry


def get_watch(session: Session, player: Player) -> WatchlistEntry | None:
    return session.get(WatchlistEntry, player.id)


def list_watchlist(session: Session, active_only: bool = True) -> Sequence[WatchlistEntry]:
    stmt = (
        select(WatchlistEntry)
        .join(WatchlistEntry.player)
        .options(joinedload(WatchlistEntry.player))
    )
    if active_only:
        stmt = stmt.where(WatchlistEntry.active.is_(True))
    return session.scalars(stmt.order_by(Player.name.is_(None), Player.name, Player.ea_id)).all()


def set_watch_active(session: Session, player: Player, active: bool) -> WatchlistEntry:
    entry = get_watch(session, player)
    if entry is None:
        raise NotFoundError(f"player {player.ea_id} is not on the watchlist")
    entry.active = active
    session.flush()
    return entry


def set_watch_interval(
    session: Session, player: Player, interval_min: int | None
) -> WatchlistEntry:
    """Collect this card every `interval_min` minutes; None means every collector run."""
    _require_positive("interval_min", interval_min)
    entry = get_watch(session, player)
    if entry is None:
        raise NotFoundError(f"player {player.ea_id} is not on the watchlist")
    entry.interval_min = interval_min
    session.flush()
    return entry


def is_due(entry: WatchlistEntry, now: datetime, tolerance: timedelta = DUE_TOLERANCE) -> bool:
    """Whether a watchlist card should be collected in the run at `now`."""
    if entry.interval_min is None or entry.checked_at is None:
        return True
    return now - entry.checked_at >= timedelta(minutes=entry.interval_min) - tolerance


def remove_watch(session: Session, player: Player) -> None:
    entry = get_watch(session, player)
    if entry is None:
        raise NotFoundError(f"player {player.ea_id} is not on the watchlist")
    session.delete(entry)
    session.flush()


# --- portfolio -------------------------------------------------------------


def open_position(
    session: Session, player: Player, buy_price: int | None, bought_at: datetime | None = None
) -> PortfolioPosition:
    """Record a purchase; `buy_price` 0 for a free card (pack, reward), None if unknown."""
    if buy_price is not None and buy_price < 0:
        raise ValueError(f"buy_price must not be negative, got {buy_price}")
    position = PortfolioPosition(
        player=player, buy_price=buy_price, bought_at=bought_at or utcnow()
    )
    session.add(position)
    session.flush()
    return position


def get_position(session: Session, position_id: int) -> PortfolioPosition:
    position = session.get(PortfolioPosition, position_id)
    if position is None:
        raise NotFoundError(f"position {position_id} not found")
    return position


def mark_listed(
    session: Session,
    position: PortfolioPosition,
    price: int,
    listed_at: datetime | None = None,
) -> PortfolioPosition:
    """Mark a position as listed at `price`; every (re)listing is kept in the history."""
    _require_positive("price", price)
    if position.status is PositionStatus.SOLD:
        raise InvalidStateError(f"position {position.id} is already sold")
    position.status = PositionStatus.LISTED
    position.sell_price = price
    position.listings.append(PortfolioListing(price=price, listed_at=listed_at or utcnow()))
    session.flush()
    return position


def sell_position(
    session: Session, position: PortfolioPosition, price: int, sold_at: datetime | None = None
) -> PortfolioPosition:
    _require_positive("price", price)
    if position.status is PositionStatus.SOLD:
        raise InvalidStateError(f"position {position.id} is already sold")
    position.status = PositionStatus.SOLD
    position.sell_price = price
    position.sold_at = sold_at or utcnow()
    session.flush()
    return position


def list_positions(
    session: Session, statuses: Sequence[PositionStatus] | None = None
) -> Sequence[PortfolioPosition]:
    stmt = select(PortfolioPosition).options(joinedload(PortfolioPosition.player))
    if statuses is not None:
        stmt = stmt.where(PortfolioPosition.status.in_(statuses))
    return session.scalars(stmt.order_by(PortfolioPosition.bought_at, PortfolioPosition.id)).all()


def open_positions_of(session: Session, player: Player) -> Sequence[PortfolioPosition]:
    """Held or listed positions of a card, oldest purchase first."""
    return session.scalars(
        select(PortfolioPosition)
        .where(
            PortfolioPosition.player_id == player.id,
            PortfolioPosition.status != PositionStatus.SOLD,
        )
        .order_by(PortfolioPosition.bought_at, PortfolioPosition.id)
    ).all()


def remove_position(session: Session, position: PortfolioPosition) -> None:
    """Delete a position that was entered by mistake."""
    session.delete(position)
    session.flush()


# --- promos ----------------------------------------------------------------


def create_promo(
    session: Session,
    name: str,
    starts_at: datetime,
    ends_at: datetime | None = None,
    confidence: float = 0.5,
    source: str | None = None,
    note: str | None = None,
) -> Promo:
    if not 0.0 <= confidence <= 1.0:
        raise ValueError(f"confidence must be between 0 and 1, got {confidence}")
    if ends_at is not None and ends_at < starts_at:
        raise ValueError("ends_at must not be before starts_at")
    promo = Promo(
        name=name,
        starts_at=starts_at,
        ends_at=ends_at,
        confidence=confidence,
        source=source,
        note=note,
    )
    session.add(promo)
    session.flush()
    return promo


def add_promo_link(session: Session, promo: Promo, link_type: LinkType, value: str) -> PromoLink:
    link = session.get(PromoLink, (promo.id, link_type, value))
    if link is None:
        link = PromoLink(promo=promo, link_type=link_type, link_value=value)
        session.add(link)
        session.flush()
    return link


def get_promo(session: Session, promo_id: int) -> Promo:
    promo = session.get(Promo, promo_id)
    if promo is None:
        raise NotFoundError(f"promo {promo_id} not found")
    return promo


def list_promos(session: Session, starting_after: datetime | None = None) -> Sequence[Promo]:
    stmt = select(Promo).options(joinedload(Promo.links))
    if starting_after is not None:
        stmt = stmt.where(Promo.starts_at >= starting_after)
    return session.scalars(stmt.order_by(Promo.starts_at, Promo.id)).unique().all()


def delete_promo(session: Session, promo: Promo) -> None:
    session.delete(promo)
    session.flush()


# --- alerts log ------------------------------------------------------------


def log_alert(
    session: Session,
    rule: str,
    message: str,
    player: Player | None = None,
    sent_at: datetime | None = None,
) -> AlertLog:
    entry = AlertLog(
        rule=rule,
        player_id=player.id if player is not None else None,
        message=message,
        sent_at=sent_at or utcnow(),
    )
    session.add(entry)
    session.flush()
    return entry


def list_alerts(session: Session, limit: int = 50) -> Sequence[AlertLog]:
    """Most recent alerts first."""
    return session.scalars(
        select(AlertLog).order_by(AlertLog.sent_at.desc(), AlertLog.id.desc()).limit(limit)
    ).all()


def last_alert(session: Session, rule: str, player: Player | None = None) -> AlertLog | None:
    """Most recent alert for a rule and player (or rule-wide alert if `player` is None)."""
    stmt = select(AlertLog).where(AlertLog.rule == rule)
    if player is None:
        stmt = stmt.where(AlertLog.player_id.is_(None))
    else:
        stmt = stmt.where(AlertLog.player_id == player.id)
    return session.scalar(stmt.order_by(AlertLog.sent_at.desc(), AlertLog.id.desc()).limit(1))


# --- source status ---------------------------------------------------------


def get_source_status(session: Session, source: str) -> SourceStatus:
    """Status row for a source, created on first use."""
    status = session.get(SourceStatus, source)
    if status is None:
        status = SourceStatus(source=source)
        session.add(status)
        session.flush()
    return status


def list_source_statuses(session: Session) -> Sequence[SourceStatus]:
    return session.scalars(select(SourceStatus).order_by(SourceStatus.source)).all()


def paused_sources(session: Session, now: datetime) -> set[str]:
    return set(
        session.scalars(select(SourceStatus.source).where(SourceStatus.paused_until > now)).all()
    )


def pause_source(session: Session, source: str, until: datetime, reason: str) -> SourceStatus:
    status = get_source_status(session, source)
    status.paused_until = until
    status.pause_reason = reason[:500]
    session.flush()
    return status


def resume_source(session: Session, source: str) -> SourceStatus:
    status = get_source_status(session, source)
    status.paused_until = None
    status.pause_reason = None
    session.flush()
    return status


def record_source_result(
    session: Session,
    source: str,
    at: datetime,
    success: bool,
    error: str | None = None,
) -> SourceStatus:
    status = get_source_status(session, source)
    if success:
        status.last_success_at = at
    if error is not None:
        status.last_error_at = at
        status.last_error = error[:500]
    session.flush()
    return status


# --- app settings ----------------------------------------------------------


def get_app_settings(session: Session, prefix: str = "") -> dict[str, str]:
    stmt = select(AppSetting)
    if prefix:
        stmt = stmt.where(AppSetting.key.startswith(prefix))
    return {row.key: row.value for row in session.scalars(stmt)}


def set_app_setting(session: Session, key: str, value: str) -> None:
    row = session.get(AppSetting, key)
    if row is None:
        session.add(AppSetting(key=key, value=value))
    else:
        row.value = value
    session.flush()
