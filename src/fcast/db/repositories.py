"""CRUD helpers on top of an SQLAlchemy session.

Functions never commit; the caller owns the transaction (see `session_scope`).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from fcast.config import Platform
from fcast.db.base import utcnow
from fcast.db.models import (
    AlertLog,
    LinkType,
    Player,
    PortfolioPosition,
    PositionStatus,
    PriceSnapshot,
    Promo,
    PromoLink,
    WatchlistEntry,
)


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


def remove_watch(session: Session, player: Player) -> None:
    entry = get_watch(session, player)
    if entry is None:
        raise NotFoundError(f"player {player.ea_id} is not on the watchlist")
    session.delete(entry)
    session.flush()


# --- portfolio -------------------------------------------------------------


def open_position(
    session: Session, player: Player, buy_price: int, bought_at: datetime | None = None
) -> PortfolioPosition:
    _require_positive("buy_price", buy_price)
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


def mark_listed(session: Session, position: PortfolioPosition, price: int) -> PortfolioPosition:
    """Mark a held position as listed on the transfer market at `price`."""
    _require_positive("price", price)
    if position.status is PositionStatus.SOLD:
        raise InvalidStateError(f"position {position.id} is already sold")
    position.status = PositionStatus.LISTED
    position.sell_price = price
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


def last_alert(session: Session, rule: str, player: Player | None = None) -> AlertLog | None:
    """Most recent alert for a rule and player (or rule-wide alert if `player` is None)."""
    stmt = select(AlertLog).where(AlertLog.rule == rule)
    if player is None:
        stmt = stmt.where(AlertLog.player_id.is_(None))
    else:
        stmt = stmt.where(AlertLog.player_id == player.id)
    return session.scalar(stmt.order_by(AlertLog.sent_at.desc(), AlertLog.id.desc()).limit(1))
