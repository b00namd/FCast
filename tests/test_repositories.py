from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from fcast.config import Platform
from fcast.db import repositories as repo
from fcast.db.models import (
    AlertLog,
    LinkType,
    PositionStatus,
    PriceSnapshot,
    PromoLink,
    WatchlistEntry,
)

T0 = datetime(2026, 9, 25, 18, 0, tzinfo=UTC)


# --- players ---------------------------------------------------------------


def test_upsert_player_creates_and_updates(session: Session) -> None:
    player = repo.upsert_player(session, 231747)
    assert player.id is not None
    assert player.name is None
    assert player.display_name == "#231747"

    same = repo.upsert_player(
        session, 231747, repo.PlayerDetails(name="Kylian Mbappé", rating=91, league="LALIGA")
    )
    assert same.id == player.id
    assert same.display_name == "Kylian Mbappé (91)"

    # None fields keep existing values.
    repo.upsert_player(session, 231747, repo.PlayerDetails(club="Real Madrid"))
    assert same.league == "LALIGA"
    assert same.club == "Real Madrid"


def test_player_updated_at_is_aware_utc(session: Session) -> None:
    player = repo.upsert_player(session, 1)
    session.commit()
    session.expire_all()
    loaded = repo.get_player(session, player.id)
    assert loaded.updated_at.tzinfo is UTC


def test_get_player_missing(session: Session) -> None:
    assert repo.get_player_by_ea_id(session, 42) is None
    with pytest.raises(repo.NotFoundError):
        repo.get_player(session, 42)


def test_invalid_ea_id(session: Session) -> None:
    with pytest.raises(ValueError, match="ea_id"):
        repo.upsert_player(session, 0)


# --- price snapshots -------------------------------------------------------


def test_snapshots_filtered_by_platform_and_time(session: Session) -> None:
    player = repo.upsert_player(session, 1)
    for hours, price in [(0, 10_000), (1, 9_500), (2, 9_800)]:
        repo.add_snapshot(
            session, player, Platform.CONSOLE, price, "manual", T0 + timedelta(hours=hours)
        )
    repo.add_snapshot(session, player, Platform.PC, 12_000, "manual", T0)

    console = repo.list_snapshots(session, player, Platform.CONSOLE)
    assert [s.price for s in console] == [10_000, 9_500, 9_800]

    recent = repo.list_snapshots(session, player, Platform.CONSOLE, since=T0 + timedelta(hours=1))
    assert [s.price for s in recent] == [9_500, 9_800]

    window = repo.list_snapshots(
        session, player, Platform.CONSOLE, since=T0, until=T0 + timedelta(minutes=30)
    )
    assert [s.price for s in window] == [10_000]

    latest = repo.latest_snapshot(session, player, Platform.CONSOLE)
    assert latest is not None
    assert latest.price == 9_800
    assert repo.latest_snapshot(session, player, Platform.PC) is not None


def test_snapshot_roundtrip_keeps_utc(session: Session) -> None:
    player = repo.upsert_player(session, 1)
    local = datetime(2026, 9, 25, 20, 0, tzinfo=UTC).astimezone()  # any aware tz
    repo.add_snapshot(session, player, Platform.CONSOLE, 1_000, "manual", local)
    session.commit()
    session.expire_all()
    stored = session.scalars(select(PriceSnapshot)).one()
    assert stored.captured_at == datetime(2026, 9, 25, 20, 0, tzinfo=UTC)
    assert stored.captured_at.tzinfo is UTC
    assert stored.platform is Platform.CONSOLE


def test_snapshot_rejects_naive_datetime(session: Session) -> None:
    player = repo.upsert_player(session, 1)
    with pytest.raises(StatementError, match="naive"):
        repo.add_snapshot(session, player, Platform.CONSOLE, 1_000, "manual", datetime(2026, 1, 1))


def test_snapshot_rejects_non_positive_price(session: Session) -> None:
    player = repo.upsert_player(session, 1)
    with pytest.raises(ValueError, match="price"):
        repo.add_snapshot(session, player, Platform.CONSOLE, 0, "manual")


def test_price_check_constraint_enforced_in_db(session: Session) -> None:
    player = repo.upsert_player(session, 1)
    session.add(PriceSnapshot(player_id=player.id, platform=Platform.PC, price=-5, source="x"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_foreign_keys_enforced(session: Session) -> None:
    session.add(PriceSnapshot(player_id=999, platform=Platform.PC, price=5, source="x"))
    with pytest.raises(IntegrityError):
        session.flush()


# --- watchlist -------------------------------------------------------------


def test_watchlist_add_update_list(session: Session) -> None:
    a = repo.upsert_player(session, 1, repo.PlayerDetails(name="Alpha"))
    b = repo.upsert_player(session, 2, repo.PlayerDetails(name="Beta"))
    repo.set_watch(session, b, target_buy=10_000, target_sell=12_000)
    repo.set_watch(session, a, target_buy=5_000, note="dip")

    entries = repo.list_watchlist(session)
    assert [e.player.name for e in entries] == ["Alpha", "Beta"]

    unnamed = repo.upsert_player(session, 3)
    repo.set_watch(session, unnamed)
    assert repo.list_watchlist(session)[-1].player is unnamed

    updated = repo.set_watch(session, a, target_buy=4_500, target_sell=6_000)
    assert updated.target_buy == 4_500
    assert updated.note is None
    assert len(session.scalars(select(WatchlistEntry)).all()) == 3


def test_watchlist_deactivate_and_reactivate(session: Session) -> None:
    player = repo.upsert_player(session, 1)
    repo.set_watch(session, player, target_buy=1_000)

    repo.set_watch_active(session, player, False)
    assert repo.list_watchlist(session) == []
    assert len(repo.list_watchlist(session, active_only=False)) == 1

    repo.set_watch(session, player, target_buy=900)
    assert len(repo.list_watchlist(session)) == 1


def test_watchlist_remove(session: Session) -> None:
    player = repo.upsert_player(session, 1)
    repo.set_watch(session, player)
    repo.remove_watch(session, player)
    assert repo.get_watch(session, player) is None
    with pytest.raises(repo.NotFoundError):
        repo.remove_watch(session, player)
    with pytest.raises(repo.NotFoundError):
        repo.set_watch_active(session, player, True)


def test_watchlist_rejects_invalid_targets(session: Session) -> None:
    player = repo.upsert_player(session, 1)
    with pytest.raises(ValueError, match="target_sell"):
        repo.set_watch(session, player, target_sell=-1)


# --- portfolio -------------------------------------------------------------


def test_position_lifecycle(session: Session) -> None:
    player = repo.upsert_player(session, 1)
    position = repo.open_position(session, player, 10_000, bought_at=T0)
    assert position.status is PositionStatus.HOLDING

    repo.mark_listed(session, position, 13_000)
    assert position.status is PositionStatus.LISTED
    assert position.sell_price == 13_000

    repo.sell_position(session, position, 12_500, sold_at=T0 + timedelta(days=1))
    assert position.status is PositionStatus.SOLD
    assert position.sell_price == 12_500
    assert position.sold_at == T0 + timedelta(days=1)

    with pytest.raises(repo.InvalidStateError):
        repo.sell_position(session, position, 14_000)
    with pytest.raises(repo.InvalidStateError):
        repo.mark_listed(session, position, 14_000)


def test_list_positions_by_status(session: Session) -> None:
    player = repo.upsert_player(session, 1)
    held = repo.open_position(session, player, 1_000, bought_at=T0)
    sold = repo.open_position(session, player, 2_000, bought_at=T0 + timedelta(hours=1))
    repo.sell_position(session, sold, 2_500)

    assert repo.list_positions(session) == [held, sold]
    open_positions = repo.list_positions(session, [PositionStatus.HOLDING, PositionStatus.LISTED])
    assert open_positions == [held]
    assert repo.get_position(session, sold.id) is sold
    with pytest.raises(repo.NotFoundError):
        repo.get_position(session, 999)


def test_player_with_position_cannot_be_deleted(session: Session) -> None:
    player = repo.upsert_player(session, 1)
    repo.open_position(session, player, 1_000)
    session.commit()
    session.delete(player)
    with pytest.raises(IntegrityError):
        session.flush()


# --- promos ----------------------------------------------------------------


def test_promo_with_links(session: Session) -> None:
    promo = repo.create_promo(
        session, "Trailblazers", T0, T0 + timedelta(days=7), confidence=0.8, source="leak"
    )
    repo.add_promo_link(session, promo, LinkType.LEAGUE, "Premier League")
    repo.add_promo_link(session, promo, LinkType.NATION, "France")
    repo.add_promo_link(session, promo, LinkType.LEAGUE, "Premier League")  # idempotent
    session.commit()
    session.expire_all()

    promos = repo.list_promos(session)
    assert len(promos) == 1
    assert {(link.link_type, link.link_value) for link in promos[0].links} == {
        (LinkType.LEAGUE, "Premier League"),
        (LinkType.NATION, "France"),
    }


def test_list_promos_filters_by_start(session: Session) -> None:
    repo.create_promo(session, "Old", T0 - timedelta(days=10))
    new = repo.create_promo(session, "New", T0 + timedelta(days=2))
    assert repo.list_promos(session, starting_after=T0) == [new]


def test_delete_promo_cascades_links(session: Session) -> None:
    promo = repo.create_promo(session, "TOTW", T0)
    repo.add_promo_link(session, promo, LinkType.CLUB, "Arsenal")
    session.commit()
    repo.delete_promo(session, repo.get_promo(session, promo.id))
    session.commit()
    assert session.scalars(select(PromoLink)).all() == []
    with pytest.raises(repo.NotFoundError):
        repo.get_promo(session, promo.id)


@pytest.mark.parametrize("confidence", [-0.1, 1.1])
def test_promo_confidence_range(session: Session, confidence: float) -> None:
    with pytest.raises(ValueError, match="confidence"):
        repo.create_promo(session, "X", T0, confidence=confidence)


def test_promo_end_before_start(session: Session) -> None:
    with pytest.raises(ValueError, match="ends_at"):
        repo.create_promo(session, "X", T0, ends_at=T0 - timedelta(hours=1))


# --- alerts log ------------------------------------------------------------


def test_last_alert_per_rule_and_player(session: Session) -> None:
    a = repo.upsert_player(session, 1)
    b = repo.upsert_player(session, 2)
    repo.log_alert(session, "BUY_DIP", "first", a, sent_at=T0)
    latest = repo.log_alert(session, "BUY_DIP", "second", a, sent_at=T0 + timedelta(hours=1))
    repo.log_alert(session, "BUY_DIP", "other player", b, sent_at=T0 + timedelta(hours=2))
    repo.log_alert(session, "SELL_TARGET", "other rule", a, sent_at=T0 + timedelta(hours=3))
    global_alert = repo.log_alert(session, "FODDER_STOCK", "no player", sent_at=T0)

    assert repo.last_alert(session, "BUY_DIP", a) == latest
    assert repo.last_alert(session, "FODDER_STOCK") == global_alert
    assert repo.last_alert(session, "FODDER_STOCK", a) is None
    assert repo.last_alert(session, "PROMO_PREBUY", a) is None


def test_alert_log_survives_player_deletion(session: Session) -> None:
    player = repo.upsert_player(session, 1)
    repo.log_alert(session, "BUY_DIP", "msg", player)
    session.commit()
    session.delete(player)
    session.commit()
    session.expire_all()
    assert session.scalars(select(AlertLog)).one().player_id is None


def test_deleting_a_watched_player_removes_dependent_rows(session: Session) -> None:
    from fcast.db.models import MarketState, SourceRef

    player = repo.upsert_player(session, 1, repo.PlayerDetails(name="Demo"))
    repo.set_watch(session, player, target_buy=1_000)
    repo.set_source_ref(session, player, "futbin", "/27/player/1/demo")
    repo.add_snapshot(session, player, Platform.PC, 1_000, "futbin", T0)
    repo.record_market_state(session, player, Platform.PC, "futbin", T0, (1_000,), 150, 5_000)
    repo.log_alert(session, "BUY_DIP", "msg", player)
    session.commit()

    session.delete(player)
    session.commit()

    assert repo.get_player_by_ea_id(session, 1) is None
    assert session.scalars(select(WatchlistEntry)).all() == []
    assert session.scalars(select(PriceSnapshot)).all() == []
    assert session.scalars(select(SourceRef)).all() == []
    assert session.scalars(select(MarketState)).all() == []
    assert session.scalars(select(AlertLog)).one().player_id is None  # history is kept
