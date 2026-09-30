"""Holo pairs: find the holographic version of watched cards and track both prices.

A holo card is a separate, rarer item with the same stats (EA id = normal id + 2^24). When the
holo trades far above the normal card, the normal card can be listed just below the holo price
("ÜV"); FCast shows the spread and signals HOLO_SPREAD.
"""

import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from fcast.collector.job import apply_player_info
from fcast.config import Settings
from fcast.db import repositories as repo
from fcast.db.models import CardPair, Player
from fcast.db.session import session_scope
from fcast.sources.base import SourceBlockedError, SourceError
from fcast.sources.futbin import (
    HOLO_ID_OFFSET,
    FutbinSource,
    is_holo_page,
    parse_card_id,
    parse_player,
    parse_versions,
)

logger = logging.getLogger(__name__)

FUTBIN = "futbin"
RECHECK = timedelta(days=2)  # holo versions appear with the card's release; check again later


def pair_of(session: Session, player: Player) -> CardPair | None:
    return session.get(CardPair, player.id)


async def discover_pairs(
    futbin: FutbinSource, factory: sessionmaker[Session], settings: Settings, now: datetime
) -> list[int]:
    """Look up holo versions of watched cards (limited per run); returns EA ids paired."""
    with session_scope(factory) as session:
        if FUTBIN in repo.paused_sources(session, now):
            return []
        todo: list[tuple[int, int, str, int | None]] = []
        holo_ids = set(
            session.scalars(
                select(CardPair.holo_player_id).where(CardPair.holo_player_id.is_not(None))
            )
        )
        for entry in repo.list_watchlist(session, active_only=True):
            player = entry.player
            ref = repo.get_source_ref(session, player.ea_id, FUTBIN)
            if ref is None or player.id in holo_ids:
                continue  # unknown page, or the card is itself a known holo
            pair = pair_of(session, player)
            if pair is not None and (pair.holo_player_id or now - pair.checked_at < RECHECK):
                continue
            todo.append((player.id, player.ea_id, ref, player.rating))
    paired: list[int] = []
    for player_id, ea_id, ref, rating in todo[: settings.holo_pairs_per_run]:
        try:
            holo = await _find_holo(futbin, ref, ea_id, rating, settings)
        except SourceBlockedError:
            break
        except SourceError as exc:
            logger.warning("holo lookup for %s failed: %s", ea_id, exc)
            continue
        with session_scope(factory) as session:
            pair = session.get(CardPair, player_id) or CardPair(player_id=player_id)
            pair.checked_at = now
            if holo is not None:
                path, html, holo_id = holo
                info = parse_player(html, holo_id, settings.platform)
                holo_player = apply_player_info(session, info)
                holo_player.card_type = f"{info.card_type or 'Spezial'} (Holo)"[:50]
                repo.set_source_ref(session, holo_player, FUTBIN, path)
                pair.holo_player_id = holo_player.id
                paired.append(ea_id)
            session.add(pair)
    return paired


async def _find_holo(
    futbin: FutbinSource, ref: str, ea_id: int, rating: int | None, settings: Settings
) -> tuple[str, str, int] | None:
    page = await futbin.get_page(ref)  # usually cached from the price run
    if is_holo_page(page):
        return None
    # The holo has the same stats, so the same rating; verify by the EA id offset.
    for version in parse_versions(page):
        if not version.holo or (rating is not None and version.rating != rating):
            continue
        html = await futbin.get_page(version.path)
        holo_id = parse_card_id(html)
        if holo_id == ea_id + HOLO_ID_OFFSET or (holo_id and is_holo_page(html)):
            return version.path, html, holo_id
    return None


def due_holo_cards(session: Session, now: datetime, settings: Settings) -> list[int]:
    """Holo partners of active watchlist cards whose price is older than the holo interval."""
    cutoff = now - timedelta(hours=settings.holo_interval_h)
    watched = [e.player for e in repo.list_watchlist(session, active_only=True)]
    due = []
    for player in watched:
        pair = pair_of(session, player)
        if pair is None or pair.holo is None:
            continue
        last = repo.latest_snapshot(session, pair.holo, settings.platform)
        if last is None or last.captured_at <= cutoff:
            due.append(pair.holo.ea_id)
    return due


def holo_partner(session: Session, player: Player) -> Player | None:
    pair = pair_of(session, player)
    return pair.holo if pair is not None else None


def normal_of(session: Session, holo: Player) -> Player | None:
    pair = session.scalar(select(CardPair).where(CardPair.holo_player_id == holo.id))
    return pair.player if pair is not None else None
