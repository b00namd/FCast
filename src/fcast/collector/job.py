"""Collector job: fetches prices for all active watchlist players and stores snapshots."""

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session, sessionmaker

from fcast.config import Platform
from fcast.db import repositories as repo
from fcast.db.base import utcnow
from fcast.db.models import Player
from fcast.db.session import session_scope
from fcast.sources.base import PlayerInfo, PlayerNotFoundError, PriceQuote, PriceSource

logger = logging.getLogger(__name__)


@dataclass
class CollectResult:
    started_at: datetime
    finished_at: datetime | None = None
    players: int = 0
    stored: int = 0
    unchanged: int = 0
    missing: list[int] = field(default_factory=list)
    errors: dict[str, list[str]] = field(default_factory=dict)

    def add_error(self, source: str, message: str) -> None:
        self.errors.setdefault(source, []).append(message)

    @property
    def error_count(self) -> int:
        return sum(len(messages) for messages in self.errors.values())


def apply_player_info(session: Session, info: PlayerInfo) -> Player:
    return repo.upsert_player(
        session,
        info.ea_id,
        repo.PlayerDetails(
            name=info.name,
            rating=info.rating,
            position=info.position,
            card_type=info.card_type,
            league=info.league,
            nation=info.nation,
            club=info.club,
        ),
    )


def store_quote(session: Session, player: Player, quote: PriceQuote) -> bool:
    """Store a quote as snapshot. Returns False if the same quote was already stored."""
    if repo.snapshot_exists(session, player, quote.platform, quote.source, quote.captured_at):
        return False
    repo.add_snapshot(session, player, quote.platform, quote.price, quote.source, quote.captured_at)
    return True


async def _first_quote(
    sources: Sequence[PriceSource], ea_id: int, platform: Platform, result: CollectResult
) -> PriceQuote | None:
    for source in sources:
        try:
            return await source.fetch_price(ea_id, platform)
        except PlayerNotFoundError:
            logger.debug("%s has no price for %s", source.name, ea_id)
        except Exception as exc:  # a broken source must never stop the collector
            logger.warning("%s failed for %s: %s", source.name, ea_id, exc)
            result.add_error(source.name, f"{ea_id}: {exc}")
    return None


async def _first_player_info(
    sources: Sequence[PriceSource], ea_id: int, result: CollectResult
) -> PlayerInfo | None:
    for source in sources:
        try:
            return await source.fetch_player(ea_id)
        except PlayerNotFoundError:
            continue
        except Exception as exc:
            logger.warning("%s player lookup failed for %s: %s", source.name, ea_id, exc)
            result.add_error(source.name, f"{ea_id} (details): {exc}")
    return None


async def collect_once(
    factory: sessionmaker[Session], sources: Sequence[PriceSource], platform: Platform
) -> CollectResult:
    result = CollectResult(started_at=utcnow())
    with session_scope(factory) as session:
        targets = [
            (entry.player.ea_id, entry.player.name is None)
            for entry in repo.list_watchlist(session, active_only=True)
        ]
    result.players = len(targets)

    for ea_id, needs_details in targets:
        info = await _first_player_info(sources, ea_id, result) if needs_details else None
        quote = await _first_quote(sources, ea_id, platform, result)
        if quote is None:
            result.missing.append(ea_id)
        # One short transaction per player: a failure only affects that player.
        try:
            with session_scope(factory) as session:
                player = repo.upsert_player(session, ea_id)
                if info is not None:
                    apply_player_info(session, info)
                if quote is not None:
                    if store_quote(session, player, quote):
                        result.stored += 1
                    else:
                        result.unchanged += 1
        except Exception as exc:
            logger.exception("storing data for %s failed", ea_id)
            result.add_error("database", f"{ea_id}: {exc}")

    result.finished_at = utcnow()
    logger.info(
        "collect run: %d players, %d stored, %d unchanged, %d without price, %d errors",
        result.players,
        result.stored,
        result.unchanged,
        len(result.missing),
        result.error_count,
    )
    return result


def import_rows(session: Session, rows: Sequence[tuple[PlayerInfo, PriceQuote]]) -> tuple[int, int]:
    """Bulk-import price history (e.g. a CSV backfill). Returns (stored, unchanged)."""
    stored = unchanged = 0
    for info, quote in rows:
        player = apply_player_info(session, info)
        if store_quote(session, player, quote):
            stored += 1
        else:
            unchanged += 1
    return stored, unchanged
