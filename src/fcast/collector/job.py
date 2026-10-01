"""Collector job: fetches prices for all active watchlist players and stores snapshots."""

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from sqlalchemy.orm import Session, sessionmaker

from fcast.analysis.market import effective_price
from fcast.config import Platform
from fcast.db import repositories as repo
from fcast.db.base import utcnow
from fcast.db.models import Player
from fcast.db.session import session_scope
from fcast.sources.base import (
    ExtinctError,
    MarketInfo,
    PlayerInfo,
    PlayerNotFoundError,
    PriceQuote,
    PriceSource,
    SourceBlockedError,
)

logger = logging.getLogger(__name__)

DEFAULT_PAUSE = timedelta(hours=24)


@dataclass
class CollectResult:
    started_at: datetime
    finished_at: datetime | None = None
    players: int = 0
    stored: int = 0
    unchanged: int = 0
    deferred: int = 0  # watchlist cards skipped because their interval has not passed
    missing: list[int] = field(default_factory=list)
    extinct: list[int] = field(default_factory=list)
    errors: dict[str, list[str]] = field(default_factory=dict)
    # Sources that started refusing us during this run, with the reason.
    paused: dict[str, str] = field(default_factory=dict)
    # Sources skipped because an earlier pause is still active.
    skipped: list[str] = field(default_factory=list)
    succeeded: set[str] = field(default_factory=set)

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
            chem_style=info.chem_style,
            chem_styles_raw=info.chem_styles,
            games_used=info.games_used,
            goals_per_game=info.goals_per_game,
        ),
    )


def store_quote(session: Session, player: Player, quote: PriceQuote) -> bool:
    """Store a quote as snapshot. Returns False if the same quote was already stored."""
    if repo.snapshot_exists(session, player, quote.platform, quote.source, quote.captured_at):
        return False
    repo.add_snapshot(session, player, quote.platform, quote.price, quote.source, quote.captured_at)
    return True


def source_order(sources: Sequence[PriceSource], offset: int) -> list[PriceSource]:
    """Local sources first, then remote sources rotated by `offset` to spread the load."""
    local = [source for source in sources if not source.remote]
    remote = [source for source in sources if source.remote]
    if remote:
        shift = offset % len(remote)
        remote = remote[shift:] + remote[:shift]
    return local + remote


def _handle_blocked(source: PriceSource, exc: SourceBlockedError, result: CollectResult) -> None:
    logger.warning("%s is refusing requests (%s), pausing it", source.name, exc)
    result.paused.setdefault(source.name, str(exc))


@dataclass
class MarketObservation:
    source: str
    observed_at: datetime
    market: MarketInfo


@dataclass
class PlayerFetch:
    quotes: list[PriceQuote] = field(default_factory=list)
    markets: list[MarketObservation] = field(default_factory=list)

    @property
    def extinct(self) -> bool:
        return any(observation.market.extinct for observation in self.markets)

    @property
    def answered_by(self) -> set[str]:
        """Sources that delivered a price or a market picture for this card."""
        return {q.source for q in self.quotes} | {m.source for m in self.markets}


def _dampen_outlier(quote: PriceQuote, outlier_gap_pct: float) -> PriceQuote:
    """Use the realistic market price when the cheapest listing is a lone outlier."""
    if quote.market is None:
        return quote
    price = effective_price(quote.market.listings, outlier_gap_pct)
    if price is None or price == quote.price:
        return quote
    logger.info(
        "%s: lowest BIN %d looks like an outlier, using %d", quote.ea_id, quote.price, price
    )
    return replace(quote, price=price)


async def _collect_quotes(
    sources: Sequence[PriceSource],
    ea_id: int,
    platform: Platform,
    result: CollectResult,
    outlier_gap_pct: float,
) -> PlayerFetch:
    """Every local source is read; of the remote sources only the first one with an answer.

    A remote source reporting "extinct" is an answer too: the fallback would only return a
    stale price, so it is not asked.
    """
    fetch = PlayerFetch()
    have_remote = False
    for source in sources:
        if source.name in result.paused or (source.remote and have_remote):
            continue
        try:
            quote = await source.fetch_price(ea_id, platform)
            if quote.market is not None:
                fetch.markets.append(
                    MarketObservation(source.name, quote.captured_at, quote.market)
                )
            fetch.quotes.append(_dampen_outlier(quote, outlier_gap_pct))
            have_remote = have_remote or source.remote
            result.succeeded.add(source.name)
        except ExtinctError as exc:
            logger.info("%s: %s reports no listings (extinct)", ea_id, source.name)
            fetch.markets.append(MarketObservation(source.name, utcnow(), exc.market))
            have_remote = have_remote or source.remote
            result.succeeded.add(source.name)
        except PlayerNotFoundError as exc:
            logger.debug("%s has no price for %s: %s", source.name, ea_id, exc)
        except SourceBlockedError as exc:
            _handle_blocked(source, exc, result)
        except Exception as exc:  # a broken source must never stop the collector
            logger.warning("%s failed for %s: %s", source.name, ea_id, exc)
            result.add_error(source.name, f"{ea_id}: {exc}")
    return fetch


async def _first_player_info(
    sources: Sequence[PriceSource], ea_id: int, result: CollectResult
) -> PlayerInfo | None:
    for source in sources:
        if source.name in result.paused:
            continue
        try:
            return await source.fetch_player(ea_id)
        except PlayerNotFoundError:
            continue
        except SourceBlockedError as exc:
            _handle_blocked(source, exc, result)
        except Exception as exc:
            logger.warning("%s player lookup failed for %s: %s", source.name, ea_id, exc)
            result.add_error(source.name, f"{ea_id} (details): {exc}")
    return None


def _save_source_statuses(
    factory: sessionmaker[Session],
    sources: Sequence[PriceSource],
    result: CollectResult,
    pause: timedelta,
) -> None:
    now = result.finished_at or utcnow()
    with session_scope(factory) as session:
        for source in sources:
            if source.name in result.skipped:
                continue
            reason = result.paused.get(source.name)
            errors = result.errors.get(source.name)
            error = reason or (errors[-1] if errors else None)
            repo.record_source_result(
                session, source.name, now, success=source.name in result.succeeded, error=error
            )
            if reason is not None:
                repo.pause_source(session, source.name, now + pause, reason)


async def collect_once(
    factory: sessionmaker[Session],
    sources: Sequence[PriceSource],
    platform: Platform,
    rotation: int = 0,
    rotate: bool = False,
    pause: timedelta = DEFAULT_PAUSE,
    outlier_gap_pct: float = 15.0,
    extra_ea_ids: Sequence[int] = (),
) -> CollectResult:
    """Collect prices for all active watchlist players.

    Remote sources are asked in the given priority order, or rotated per player and run when
    `rotate` is set. A source that refuses us (403/429/bot challenge) is paused for `pause`
    and not contacted again until then.
    """
    result = CollectResult(started_at=utcnow())
    with session_scope(factory) as session:
        paused_now = repo.paused_sources(session, result.started_at)
        entries = repo.list_watchlist(session, active_only=True)
        due = [entry for entry in entries if repo.is_due(entry, result.started_at)]
        result.deferred = len(entries) - len(due)
        targets = [(entry.player.ea_id, entry.player.name is None) for entry in due]
        due_ids = {ea_id for ea_id, _ in targets}
        # Extra cards (e.g. the promo candidate pool) are priced like watchlist cards.
        watched = {entry.player.ea_id for entry in entries}
        for ea_id in extra_ea_ids:
            if ea_id not in watched:
                player = repo.get_player_by_ea_id(session, ea_id)
                targets.append((ea_id, player is None or player.name is None))
    result.skipped = sorted(source.name for source in sources if source.name in paused_now)
    for name in result.skipped:
        logger.info("%s is paused, skipping it", name)
    active = [source for source in sources if source.name not in paused_now]
    result.players = len(targets)

    for index, (ea_id, needs_details) in enumerate(targets):
        ordered = source_order(active, rotation + index if rotate else 0)
        fetch = await _collect_quotes(ordered, ea_id, platform, result, outlier_gap_pct)
        # After the price so that remote sources can answer from their page cache. Unknown
        # cards ask every source; known cards only refresh usage data (chem style, games)
        # from the remote source that just answered, which costs no extra request.
        answered = fetch.answered_by
        detail_sources = (
            sorted(ordered, key=lambda source: source.name not in answered)
            if needs_details
            else [s for s in ordered if s.remote and s.name in answered]
        )
        info = await _first_player_info(detail_sources, ea_id, result)
        if fetch.extinct:
            result.extinct.append(ea_id)
        elif not fetch.quotes:
            result.missing.append(ea_id)
        # One short transaction per player: a failure only affects that player.
        try:
            with session_scope(factory) as session:
                player = repo.upsert_player(session, ea_id)
                if ea_id in due_ids:
                    entry = repo.get_watch(session, player)
                    if entry is not None:
                        entry.checked_at = result.started_at
                if info is not None:
                    apply_player_info(session, info)
                for observation in fetch.markets:
                    repo.record_market_state(
                        session,
                        player,
                        platform,
                        observation.source,
                        observation.observed_at,
                        observation.market.listings,
                        observation.market.range_min,
                        observation.market.range_max,
                    )
                for quote in fetch.quotes:
                    if store_quote(session, player, quote):
                        result.stored += 1
                    else:
                        result.unchanged += 1
        except Exception as exc:
            logger.exception("storing data for %s failed", ea_id)
            result.add_error("database", f"{ea_id}: {exc}")

    result.finished_at = utcnow()
    try:
        _save_source_statuses(factory, active, result, pause)
    except Exception:
        logger.exception("saving source status failed")
    logger.info(
        "collect run: %d players (%d not due), %d stored, %d unchanged, %d extinct, "
        "%d without price, %d errors%s",
        result.players,
        result.deferred,
        result.stored,
        result.unchanged,
        len(result.extinct),
        len(result.missing),
        result.error_count,
        f", paused: {', '.join(result.paused)}" if result.paused else "",
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
