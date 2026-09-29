"""Read models for the dashboard pages (no HTTP concerns here)."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from fcast.config import Platform
from fcast.db import repositories as repo
from fcast.db.models import Player, PriceSnapshot, WatchlistEntry

CHANGE_WINDOW = timedelta(hours=24)
CHART_WINDOW = timedelta(days=7)


@dataclass(frozen=True)
class PriceRow:
    entry: WatchlistEntry
    latest: PriceSnapshot | None
    previous: PriceSnapshot | None  # newest snapshot at least 24 h old

    @property
    def player(self) -> Player:
        return self.entry.player

    @property
    def change_pct(self) -> float | None:
        if self.latest is None or self.previous is None:
            return None
        return (self.latest.price - self.previous.price) / self.previous.price * 100

    @property
    def buy_hit(self) -> bool:
        target = self.entry.target_buy
        return target is not None and self.latest is not None and self.latest.price <= target

    @property
    def sell_hit(self) -> bool:
        target = self.entry.target_sell
        return target is not None and self.latest is not None and self.latest.price >= target


def price_overview(session: Session, platform: Platform, now: datetime) -> list[PriceRow]:
    rows = []
    for entry in repo.list_watchlist(session, active_only=True):
        rows.append(
            PriceRow(
                entry=entry,
                latest=repo.latest_snapshot(session, entry.player, platform),
                previous=repo.snapshot_before(session, entry.player, platform, now - CHANGE_WINDOW),
            )
        )
    return rows


@dataclass(frozen=True)
class PriceStats:
    count: int
    low: int
    high: int
    average: int

    @classmethod
    def of(cls, snapshots: Sequence[PriceSnapshot]) -> "PriceStats | None":
        if not snapshots:
            return None
        prices = [snapshot.price for snapshot in snapshots]
        return cls(
            count=len(prices),
            low=min(prices),
            high=max(prices),
            average=round(sum(prices) / len(prices)),
        )


@dataclass
class PlayerDetail:
    player: Player
    entry: WatchlistEntry | None
    refs: dict[str, str]
    snapshots: Sequence[PriceSnapshot]  # chart window, oldest first
    stats: PriceStats | None
    chart: dict[str, list[tuple[int, int]]] = field(default_factory=dict)

    @property
    def latest(self) -> PriceSnapshot | None:
        return self.snapshots[-1] if self.snapshots else None

    @property
    def recent(self) -> list[PriceSnapshot]:
        return list(reversed(self.snapshots))[:20]


def player_detail(
    session: Session, ea_id: int, platform: Platform, now: datetime
) -> PlayerDetail | None:
    player = repo.get_player_by_ea_id(session, ea_id)
    if player is None:
        return None
    snapshots = repo.list_snapshots(session, player, platform, since=now - CHART_WINDOW)
    chart: dict[str, list[tuple[int, int]]] = {}
    for snapshot in snapshots:
        point = (int(snapshot.captured_at.timestamp() * 1000), snapshot.price)
        chart.setdefault(snapshot.source, []).append(point)
    return PlayerDetail(
        player=player,
        entry=repo.get_watch(session, player),
        refs=repo.list_source_refs(session, player),
        snapshots=snapshots,
        stats=PriceStats.of(snapshots),
        chart=chart,
    )
