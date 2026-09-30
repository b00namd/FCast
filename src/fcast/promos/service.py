"""Leak & promo radar workflow: feeds, promos, candidate pool, link scoring, signals."""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, sessionmaker

from fcast.analysis import signals as sig
from fcast.analysis.stats import price_stats
from fcast.collector.job import apply_player_info
from fcast.config import Settings
from fcast.db import repositories as repo
from fcast.db.models import LeakItem, LinkType, Player, PoolCard, Promo
from fcast.db.session import session_scope
from fcast.promos.entities import Detection, detect
from fcast.promos.feeds import is_relevant, is_relevant_text, parse_feed_setting, parse_rss
from fcast.promos.scoring import (
    CardInfo,
    PromoInfo,
    PromoMatch,
    PromoWeights,
    best_matches,
    is_prebuy,
)
from fcast.sources.base import SourceBlockedError, SourceError
from fcast.sources.futbin import FutbinSource, parse_card_id, parse_player
from fcast.sources.futbin_locator import YEAR
from fcast.sources.http import PoliteHttpClient

logger = logging.getLogger(__name__)

FUTBIN = "futbin"
LOOKAHEAD = timedelta(days=14)  # promos starting later than this are not scored yet
POOL_GRACE = timedelta(days=3)  # keep tracking pool cards a bit after the promo ended
HISTORY_WINDOW = timedelta(days=30)


# --- leak inbox ------------------------------------------------------------------


async def sync_feeds(
    client: PoliteHttpClient, factory: sessionmaker[Session], settings: Settings
) -> int:
    """Fetch the configured feeds and store new relevant items; returns how many are new."""
    new = 0
    for name, url in parse_feed_setting(settings.leak_feeds):
        try:
            items = [
                item for item in parse_rss(await client.get_text(url), name) if is_relevant(item)
            ]
        except SourceError as exc:
            logger.warning("feed %s failed: %s", name, exc)
            continue
        with session_scope(factory) as session:
            known = set(session.scalars(select(LeakItem.guid).where(LeakItem.source == name)).all())
            for item in items:
                if item.guid in known:
                    continue
                session.add(
                    LeakItem(
                        source=name,
                        guid=item.guid[:300],
                        title=item.title,
                        url=item.url[:500],
                        published_at=item.published,
                        summary=item.summary,
                        is_leak=item.is_leak,
                    )
                )
                new += 1
    return new


def inbox(session: Session, limit: int = 30) -> list[LeakItem]:
    """New items, newest first; the relevance filter is re-applied so it can be improved."""
    items = session.scalars(
        select(LeakItem)
        .where(LeakItem.status == "new")
        .order_by(LeakItem.published_at.desc().nulls_last(), LeakItem.id.desc())
        .limit(limit * 3)
    )
    return [item for item in items if is_relevant_text(item.title, item.summary)][:limit]


# --- promos ----------------------------------------------------------------------


def known_clubs(session: Session) -> list[str]:
    return sorted({c for c in session.scalars(select(Player.club)) if c})


async def analyze_text(text: str, futbin: FutbinSource | None, session: Session) -> Detection:
    slugs: list[str] = []
    if futbin is not None:
        try:
            slugs = list((await futbin.locator().index()).keys())
        except SourceError as exc:
            logger.warning("FUTBIN index unavailable: %s", exc)
    return detect(text, slugs, known_clubs(session))


def create_promo(
    session: Session,
    name: str,
    starts_at: datetime,
    ends_at: datetime | None,
    confidence: float,
    source: str | None,
    note: str | None,
    links: list[tuple[LinkType, str]],
    leak_id: int | None = None,
) -> Promo:
    promo = repo.create_promo(session, name, starts_at, ends_at, confidence, source, note)
    for link_type, value in links:
        repo.add_promo_link(session, promo, link_type, value)
    if leak_id is not None:
        leak = session.get(LeakItem, leak_id)
        if leak is not None:
            leak.status = "used"
            leak.promo_id = promo.id
    session.flush()
    return promo


async def fill_pool(
    futbin: FutbinSource, factory: sessionmaker[Session], promo_id: int, settings: Settings
) -> list[int]:
    """Find the base cards of the leaked players and track them in the candidate pool."""
    with session_scope(factory) as session:
        promo = session.get(Promo, promo_id)
        if promo is None:
            return []
        slugs = [link.link_value for link in promo.links if link.link_type is LinkType.PLAYER]
        until = (promo.ends_at or promo.starts_at + timedelta(days=7)) + POOL_GRACE
        name = promo.name
    added: list[int] = []
    index = await futbin.locator().index()
    for slug in slugs:
        ids = sorted(index.get(slug, []))
        if not ids:
            continue
        path = f"/{YEAR}/player/{ids[0]}/{slug}"  # oldest FUTBIN id = base card
        try:
            html = await futbin.get_page(path)
        except SourceBlockedError:
            raise
        except SourceError as exc:
            logger.warning("pool lookup %s failed: %s", slug, exc)
            continue
        ea_id = parse_card_id(html)
        if ea_id is None:
            continue
        info = parse_player(html, ea_id, settings.platform)
        with session_scope(factory) as session:
            player = apply_player_info(session, info)
            repo.set_source_ref(session, player, FUTBIN, path)
            pool = session.get(PoolCard, player.id) or PoolCard(player_id=player.id)
            pool.promo_id = promo_id
            pool.reason = f"Leak: {name}"[:200]
            pool.until = max(pool.until, until) if pool.until else until
            session.add(pool)
        added.append(ea_id)
    return added


def due_pool_cards(session: Session, now: datetime, settings: Settings) -> list[int]:
    """Pool cards (not on the watchlist) whose last price is older than the pool interval."""
    watched = {e.player_id for e in repo.list_watchlist(session, active_only=True)}
    due: list[tuple[datetime, int]] = []
    cutoff = now - timedelta(hours=settings.pool_interval_h)
    pool = session.scalars(select(PoolCard).options(joinedload(PoolCard.player)))
    for card in pool:
        if card.until < now or card.player_id in watched:
            continue
        last = repo.latest_snapshot(session, card.player, settings.platform)
        last_at = last.captured_at if last is not None else datetime.min.replace(tzinfo=now.tzinfo)
        if last_at <= cutoff:
            due.append((last_at, card.player.ea_id))
    return [ea_id for _, ea_id in sorted(due)[: settings.pool_per_run]]


# --- candidates & signals ------------------------------------------------------------


def active_promos(session: Session, now: datetime) -> list[PromoInfo]:
    promos = []
    for promo in repo.list_promos(session):
        if promo.starts_at > now + LOOKAHEAD:
            continue
        if promo.ends_at is not None and promo.ends_at < now:
            continue
        promos.append(
            PromoInfo(
                promo_id=promo.id,
                name=promo.name,
                starts_at=promo.starts_at,
                ends_at=promo.ends_at,
                confidence=promo.confidence,
                links=tuple((link.link_type.value, link.link_value) for link in promo.links),
            )
        )
    return promos


@dataclass
class Candidate:
    match: PromoMatch
    player: Player
    price: int | None
    in_pool: bool
    on_watchlist: bool


def candidates(session: Session, settings: Settings, now: datetime) -> list[Candidate]:
    promos = active_promos(session, now)
    if not promos:
        return []
    weights = PromoWeights.from_settings(settings)
    pool_ids = set(session.scalars(select(PoolCard.player_id).where(PoolCard.until >= now)))
    watched = {e.player_id for e in repo.list_watchlist(session, active_only=True)}
    players = session.scalars(select(Player).where(Player.id.in_(pool_ids | watched))).all()
    cards = []
    by_ea: dict[int, tuple[Player, int | None]] = {}
    for player in players:
        snapshots = repo.list_snapshots(
            session, player, settings.platform, since=now - HISTORY_WINDOW
        )
        stats = (
            price_stats([(s.captured_at, s.price) for s in snapshots], now) if snapshots else None
        )
        ref = repo.get_source_ref(session, player.ea_id, FUTBIN)
        slug = ref.rsplit("/", 1)[-1] if ref else None
        cards.append(
            (
                CardInfo(
                    ea_id=player.ea_id,
                    name=player.name or str(player.ea_id),
                    slug=slug,
                    club=player.club,
                    league=player.league,
                    nation=player.nation,
                ),
                stats,
            )
        )
        by_ea[player.ea_id] = (player, stats.current if stats else None)
    result = []
    for match in best_matches(cards, promos, now, weights):
        player, price = by_ea[match.card.ea_id]
        result.append(
            Candidate(
                match=match,
                player=player,
                price=price,
                in_pool=player.id in pool_ids,
                on_watchlist=player.id in watched,
            )
        )
    return result


def prebuy_signals(session: Session, settings: Settings, now: datetime) -> list[sig.Signal]:
    weights = PromoWeights.from_settings(settings)
    found = []
    for candidate in candidates(session, settings, now):
        match = candidate.match
        if not is_prebuy(match, weights):
            continue
        days = round(match.days_to_start)
        found.append(
            sig.Signal(
                rule=sig.Rule.PROMO_PREBUY,
                ea_id=candidate.player.ea_id,
                name=candidate.player.display_name,
                price=candidate.price,
                reference=None,
                recommended=candidate.price,
                expected_profit=None,
                score=match.score,
                reasons=(f"{match.promo.name} startet in ~{days} Tagen", *match.reasons),
            )
        )
    return found
