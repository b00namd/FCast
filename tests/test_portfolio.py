import pytest
from sqlalchemy.orm import Session

from fcast.analysis.pricing import break_even_sell_price, profit
from fcast.db import repositories as repo
from fcast.db.models import Player, PositionStatus


@pytest.fixture
def player(session: Session) -> Player:
    return repo.upsert_player(session, ea_id=231747, details=repo.PlayerDetails(name="Test Card"))


def test_open_position_starts_as_holding(session: Session, player: Player) -> None:
    position = repo.open_position(session, player, 16_000)

    assert position.id is not None
    assert position.status is PositionStatus.HOLDING
    assert position.buy_price == 16_000
    assert position.sell_price is None
    assert position.sold_at is None


def test_open_position_rejects_non_positive_price(session: Session, player: Player) -> None:
    with pytest.raises(ValueError):
        repo.open_position(session, player, 0)


def test_mark_listed_keeps_position_open(session: Session, player: Player) -> None:
    position = repo.open_position(session, player, 16_000)

    repo.mark_listed(session, position, 19_000)

    assert position.status is PositionStatus.LISTED
    assert position.sell_price == 19_000
    assert position.sold_at is None


def test_sell_position_records_the_sale(session: Session, player: Player) -> None:
    position = repo.open_position(session, player, 16_000)

    repo.sell_position(session, position, 19_000)

    assert position.status is PositionStatus.SOLD
    assert position.sell_price == 19_000
    assert position.sold_at is not None


def test_selling_twice_is_rejected(session: Session, player: Player) -> None:
    position = repo.open_position(session, player, 16_000)
    repo.sell_position(session, position, 19_000)

    with pytest.raises(repo.InvalidStateError):
        repo.sell_position(session, position, 20_000)


def test_profit_accounts_for_the_ea_tax(session: Session, player: Player) -> None:
    """A sale just above the buy price is a loss once EA takes its 5 %."""
    position = repo.open_position(session, player, 16_000)
    repo.sell_position(session, position, 16_500)

    assert position.sell_price is not None
    assert profit(position.buy_price, position.sell_price) < 0
    assert profit(position.buy_price, break_even_sell_price(position.buy_price)) >= 0


def test_list_positions_filters_by_status(session: Session, player: Player) -> None:
    held = repo.open_position(session, player, 16_000)
    sold = repo.open_position(session, player, 10_000)
    repo.sell_position(session, sold, 12_000)

    assert [p.id for p in repo.list_positions(session, [PositionStatus.HOLDING])] == [held.id]
    assert [p.id for p in repo.list_positions(session, [PositionStatus.SOLD])] == [sold.id]
    assert len(repo.list_positions(session)) == 2


def test_get_position_raises_for_unknown_id(session: Session) -> None:
    with pytest.raises(repo.NotFoundError):
        repo.get_position(session, 404)


def test_remove_position(session: Session, player: Player) -> None:
    position = repo.open_position(session, player, 16_000)

    repo.remove_position(session, position)

    assert repo.list_positions(session) == []
