"""TOTW prediction workflow: sync matches, score, link FC cards, evaluate, alert."""

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, time, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from fcast.alerts.notifier import Notification, Notifier, NotifierError, Priority
from fcast.config import Settings
from fcast.db import repositories as repo
from fcast.db.base import utcnow
from fcast.db.models import ExternalCard, RealMatch, TotwActual, TotwPrediction
from fcast.db.session import session_scope
from fcast.sources.base import SourceBlockedError, SourceError
from fcast.sources.futbin import (
    FutbinSource,
    parse_card_id,
    parse_player,
    parse_price,
)
from fcast.sources.futbin_locator import YEAR
from fcast.totw import calendar
from fcast.totw.futbin_totw import parse_totw_page, totw_path
from fcast.totw.names import clubs_match, last_name, matches_slug
from fcast.totw.openligadb import Goal, Match, OpenLigaDbClient
from fcast.totw.scoring import Candidate, TotwWeights, score_candidates

logger = logging.getLogger(__name__)

SOURCE = "openligadb"
TOP_STORED = 25  # candidates stored per week
PAGES_PER_CARD = 4  # FUTBIN pages checked per candidate at most
CARD_RECHECK = timedelta(days=14)  # retry cards that were not found
ALERT_BEFORE = timedelta(hours=20)  # alert window before the release
ALERT_TOP = 5
FUTBIN = "futbin"


def current_season(now: datetime) -> int:
    """OpenLigaDB season = year the season started (2026 for 2026/27)."""
    return now.year if now.month >= 7 else now.year - 1


def _to_row(match: Match) -> dict[str, object]:
    return {
        "source": SOURCE,
        "ext_id": match.ext_id,
        "league": match.league,
        "season": match.season,
        "matchday": match.matchday,
        "kickoff": match.kickoff,
        "home_id": match.home_id,
        "home": match.home,
        "away_id": match.away_id,
        "away": match.away,
        "finished": match.finished,
        "home_goals": match.home_goals,
        "away_goals": match.away_goals,
        "goals_json": json.dumps([asdict(goal) for goal in match.goals]),
    }


def _from_row(row: RealMatch) -> Match:
    return Match(
        ext_id=row.ext_id,
        league=row.league,
        season=row.season,
        matchday=row.matchday,
        kickoff=row.kickoff,
        home_id=row.home_id,
        home=row.home,
        away_id=row.away_id,
        away=row.away,
        finished=row.finished,
        home_goals=row.home_goals,
        away_goals=row.away_goals,
        goals=tuple(Goal(**goal) for goal in json.loads(row.goals_json)),
    )


@dataclass
class TotwReport:
    week: int
    matches: int = 0
    candidates: int = 0
    linked: int = 0
    evaluated_week: int | None = None
    alert_sent: bool = False
    errors: list[str] = field(default_factory=list)


class TotwService:
    def __init__(
        self,
        settings: Settings,
        factory: sessionmaker[Session],
        openligadb: OpenLigaDbClient,
        futbin: FutbinSource | None,
        notifier: Notifier | None = None,
    ) -> None:
        self.settings = settings
        self.factory = factory
        self.openligadb = openligadb
        self.futbin = futbin
        self.notifier = notifier
        self.weights = TotwWeights.from_settings(settings)
        hours, minutes = settings.totw_release_time.split(":")
        self.release_time = time(int(hours), int(minutes))

    # --- calendar ------------------------------------------------------------

    def upcoming(self, now: datetime) -> calendar.TotwWeek:
        return calendar.upcoming_week(
            now, self.settings.totw_first_release, self.release_time, self.settings.tz
        )

    def last_released(self, now: datetime) -> calendar.TotwWeek | None:
        return calendar.last_released_week(
            now, self.settings.totw_first_release, self.release_time, self.settings.tz
        )

    def week(self, number: int) -> calendar.TotwWeek:
        return calendar.week(
            self.settings.totw_first_release, self.release_time, self.settings.tz, number
        )

    # --- matches -----------------------------------------------------------------

    async def sync_matches(self, now: datetime) -> int:
        """Fetch the current and previous matchday of every configured league."""
        season = current_season(now)
        stored = 0
        for league in [x.strip() for x in self.settings.totw_leagues.split(",") if x.strip()]:
            try:
                current = await self.openligadb.current_matchday(league)
                matches: list[Match] = []
                for matchday in {max(1, current - 1), current}:
                    matches += await self.openligadb.matchday(league, season, matchday)
            except SourceError as exc:
                logger.warning("OpenLigaDB %s failed: %s", league, exc)
                continue
            with session_scope(self.factory) as session:
                for match in matches:
                    row = session.scalar(
                        select(RealMatch).where(
                            RealMatch.source == SOURCE, RealMatch.ext_id == match.ext_id
                        )
                    )
                    values = _to_row(match)
                    if row is None:
                        session.add(RealMatch(**values))
                    else:
                        for key, value in values.items():
                            setattr(row, key, value)
                    stored += 1
        return stored

    def matches_in(self, session: Session, week: calendar.TotwWeek) -> list[Match]:
        rows = session.scalars(
            select(RealMatch).where(
                RealMatch.kickoff >= week.window_start, RealMatch.kickoff < week.window_end
            )
        )
        return [_from_row(row) for row in rows]

    def predict(self, session: Session, week: calendar.TotwWeek) -> list[Candidate]:
        return score_candidates(self.matches_in(session, week), self.weights)

    # --- FC cards ----------------------------------------------------------------

    async def link_card(self, candidate: Candidate) -> ExternalCard | None:
        """Find the base FC card of a scorer on FUTBIN (cached per player)."""
        now = utcnow()
        with session_scope(self.factory) as session:
            cached = session.get(ExternalCard, candidate.key)
            if cached is not None and (cached.futbin_ref or now - cached.checked_at < CARD_RECHECK):
                session.expunge(cached)
                return cached
        if self.futbin is None:
            return None
        surname = last_name(candidate.name)
        index = await self.futbin.locator().index()
        slugs = [
            slug
            for slug in index
            if surname
            and (slug == surname or slug.endswith(f"-{surname}"))
            and matches_slug(candidate.name, slug)
        ]
        # Base cards have the oldest FUTBIN ids.
        paths = sorted((futbin_id, slug) for slug in slugs for futbin_id in index[slug])[
            :PAGES_PER_CARD
        ]
        found: tuple[str, int] | None = None
        for futbin_id, slug in paths:
            path = f"/{YEAR}/player/{futbin_id}/{slug}"
            html = await self.futbin.get_page(path)
            info = parse_player(html, 0)
            if clubs_match(info.club, candidate.team):
                card_id = parse_card_id(html)
                if card_id is not None:
                    found = (path, card_id)
                    break
        with session_scope(self.factory) as session:
            row = session.get(ExternalCard, candidate.key) or ExternalCard(key=candidate.key)
            row.futbin_ref, row.ea_id = found if found else (None, None)
            row.checked_at = now
            session.add(row)
            session.flush()
            session.expunge(row)
            return row

    async def card_snapshot(self, card: ExternalCard) -> tuple[str | None, int | None, str | None]:
        """(card name, current price on the configured platform, chem styles) from FUTBIN."""
        if self.futbin is None or card.futbin_ref is None:
            return None, None, None
        html = await self.futbin.get_page(card.futbin_ref)
        info = parse_player(html, card.ea_id or 0, self.settings.platform)
        try:
            price: int | None = parse_price(html, self.settings.platform).price
        except SourceError:  # includes ExtinctError: no listing, no price
            price = None
        name = f"{info.name} ({info.rating})" if info.name and info.rating else info.name
        return name, price, info.chem_styles

    # --- evaluation ----------------------------------------------------------------

    async def evaluate(self, week: calendar.TotwWeek) -> bool:
        """Store the actual TOTW of a released week once (from FUTBIN)."""
        if self.futbin is None:
            return False
        with session_scope(self.factory) as session:
            if session.scalar(select(TotwActual.futbin_id).where(TotwActual.week == week.number)):
                return False
        html = await self.futbin.get_page(totw_path(week.number))
        players = parse_totw_page(html)
        if not players:
            return False
        with session_scope(self.factory) as session:
            for player in players:
                session.add(
                    TotwActual(
                        week=week.number,
                        futbin_id=player.futbin_id,
                        slug=player.slug,
                        name=player.name,
                    )
                )
        return True

    # --- alert -------------------------------------------------------------------

    async def maybe_alert(self, week: calendar.TotwWeek, now: datetime) -> bool:
        from fcast.alerts.config import load_alert_config

        if self.notifier is None or not (week.release - ALERT_BEFORE <= now < week.release):
            return False
        rule = f"TOTW:{week.number}"
        with session_scope(self.factory) as session:
            config = load_alert_config(session, self.settings)
            if not (config.enabled and config.totw) or config.is_quiet(now, self.settings.tz):
                return False
            if repo.last_alert(session, rule) is not None:
                return False
            top = session.scalars(
                select(TotwPrediction)
                .where(TotwPrediction.week == week.number)
                .order_by(TotwPrediction.rank)
                .limit(ALERT_TOP)
            ).all()
            if not top:
                return False
            lines = []
            for p in top:
                price = f" · {p.price:,} Coins".replace(",", ".") if p.price else ""
                lines.append(f"{p.rank}. {p.name} ({p.team}) - {p.reasons}{price}")
        release_local = week.release.astimezone(self.settings.tz).strftime("%a %H:%M")
        base = self.settings.dashboard_url
        click = f"{base.rstrip('/')}/totw" if base else None
        notification = Notification(
            title=f"TOTW {week.number}: Kandidaten vor dem Release ({release_local})",
            message="\n".join(lines),
            priority=Priority.DEFAULT,
            tags=("soccer",),
            click_url=click,
            actions=(("TOTW-Prognose", click),) if click else (),
        )
        try:
            await self.notifier.send(notification)
        except NotifierError as exc:
            logger.warning("TOTW alert not delivered: %s", exc)
            return False
        with session_scope(self.factory) as session:
            repo.log_alert(
                session, rule, f"{notification.title}\n{notification.message}", sent_at=now
            )
        return True

    # --- the whole run -----------------------------------------------------------

    async def refresh(self, now: datetime | None = None) -> TotwReport:
        now = now or utcnow()
        week = self.upcoming(now)
        report = TotwReport(week=week.number)
        report.matches = await self.sync_matches(now)

        with session_scope(self.factory) as session:
            candidates = self.predict(session, week)[:TOP_STORED]
            futbin_paused = FUTBIN in repo.paused_sources(session, now)
        report.candidates = len(candidates)

        cards: dict[str, ExternalCard | None] = {}
        snapshots: dict[str, tuple[str | None, int | None, str | None]] = {}
        if not futbin_paused:
            for candidate in candidates[: self.settings.totw_card_lookups]:
                try:
                    card = await self.link_card(candidate)
                    cards[candidate.key] = card
                    if card is not None and card.futbin_ref:
                        snapshots[candidate.key] = await self.card_snapshot(card)
                        report.linked += 1
                except SourceBlockedError as exc:
                    report.errors.append(f"FUTBIN: {exc}")
                    break
                except SourceError as exc:
                    report.errors.append(f"{candidate.name}: {exc}")

        with session_scope(self.factory) as session:
            session.execute(delete(TotwPrediction).where(TotwPrediction.week == week.number))
            for rank, candidate in enumerate(candidates, start=1):
                card = cards.get(candidate.key)
                name, price, chems = snapshots.get(candidate.key, (None, None, None))
                session.add(
                    TotwPrediction(
                        week=week.number,
                        key=candidate.key,
                        rank=rank,
                        name=candidate.name,
                        team=candidate.team,
                        league=candidate.league,
                        score=candidate.score,
                        goals=candidate.goals,
                        reasons=candidate.reasons[:200],
                        ea_id=card.ea_id if card else None,
                        futbin_ref=card.futbin_ref if card else None,
                        card_name=name,
                        price=price,
                        chem_styles_raw=chems,
                    )
                )

        released = self.last_released(now)
        if released is not None and not futbin_paused:
            try:
                if await self.evaluate(released):
                    report.evaluated_week = released.number
            except SourceError as exc:
                report.errors.append(f"TOTW {released.number}: {exc}")
        report.alert_sent = await self.maybe_alert(week, now)
        logger.info(
            "TOTW %d: %d matches, %d candidates, %d cards%s",
            week.number,
            report.matches,
            report.candidates,
            report.linked,
            f", errors: {report.errors}" if report.errors else "",
        )
        return report


def hit_rate(session: Session, number: int, top: int = 10) -> tuple[int, int, list[str]] | None:
    """(hits, predicted, hit names) of a released week; None if not evaluated or not predicted."""
    actual = session.scalars(select(TotwActual).where(TotwActual.week == number)).all()
    if not actual:
        return None
    predicted = session.scalars(
        select(TotwPrediction)
        .where(TotwPrediction.week == number)
        .order_by(TotwPrediction.rank)
        .limit(top)
    ).all()
    if not predicted:
        return None  # FCast made no prediction for that week
    hits = [p.name for p in predicted if any(matches_slug(p.name, a.slug) for a in actual)]
    return len(hits), len(predicted), hits
