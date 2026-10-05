from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session, sessionmaker

from fcast.analysis import signals as sig
from fcast.db import repositories as repo
from fcast.db.base import Base
from fcast.db.session import create_db_engine, create_session_factory
from fcast.portfolio import coins as wallet

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


@pytest.fixture
def factory() -> sessionmaker[Session]:
    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def test_no_balance_until_entered(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        assert wallet.balance(session) is None
        assert wallet.affordable(5_000_000, None)


def test_trades_after_entering_carry_the_balance_forward(factory: sessionmaker[Session]) -> None:
    with factory.begin() as session:
        player = repo.upsert_player(session, 1, repo.PlayerDetails(name="Card"))
        before = repo.open_position(session, player, 20_000, bought_at=T0 - timedelta(days=1))
        wallet.set_balance(session, 100_000, now=T0)
        repo.open_position(session, player, 30_000, bought_at=T0 + timedelta(hours=1))
        # Bought before the balance was entered, sold after: only the sale counts.
        repo.sell_position(session, before, 40_000, sold_at=T0 + timedelta(hours=2))

    with factory() as session:
        balance = wallet.balance(session)
    assert balance is not None
    assert (balance.spent, balance.received) == (30_000, 38_000)  # 40.000 minus 5 % tax
    assert balance.coins == 100_000 - 30_000 + 38_000


def test_entering_again_resets_and_deleting_corrects(factory: sessionmaker[Session]) -> None:
    with factory.begin() as session:
        player = repo.upsert_player(session, 1, repo.PlayerDetails(name="Card"))
        wallet.set_balance(session, 50_000, now=T0)
        wrong = repo.open_position(session, player, 10_000, bought_at=T0 + timedelta(hours=1))
        assert wallet.balance(session).coins == 40_000  # type: ignore[union-attr]
        repo.remove_position(session, wrong)
        assert wallet.balance(session).coins == 50_000  # type: ignore[union-attr]
        wallet.set_balance(session, 70_000, now=T0 + timedelta(hours=3))
        assert wallet.balance(session).coins == 70_000  # type: ignore[union-attr]


def test_negative_balance_is_rejected(factory: sessionmaker[Session]) -> None:
    with factory() as session, pytest.raises(ValueError):
        wallet.set_balance(session, -1)


def _signal(rule: sig.Rule, price: int, expected: int | None = 500) -> sig.Signal:
    return sig.Signal(rule, 1, "Card", price, price, price, expected, None, ())


def test_buy_price_only_for_signals_that_need_coins() -> None:
    assert wallet.buy_price(_signal(sig.Rule.BUY_DIP, 8_000)) == 8_000
    assert wallet.buy_price(_signal(sig.Rule.PROMO_PREBUY, 8_000)) == 8_000
    assert wallet.buy_price(_signal(sig.Rule.OVERPRICE_CHANCE, 8_000)) == 8_000
    # Extinct ÜV chance, sell target and holo: about cards you already own.
    assert wallet.buy_price(_signal(sig.Rule.OVERPRICE_CHANCE, 8_000, expected=None)) is None
    assert wallet.buy_price(_signal(sig.Rule.SELL_TARGET, 8_000)) is None
    assert wallet.buy_price(_signal(sig.Rule.HOLO_SPREAD, 8_000)) is None


def test_affordable_up_to_the_full_balance() -> None:
    balance = wallet.CoinBalance(10_000, 10_000, T0, 0, 0)
    assert wallet.affordable(10_000, balance)
    assert not wallet.affordable(10_001, balance)
    assert wallet.affordable(None, balance)
