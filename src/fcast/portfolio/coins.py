"""Coin balance: a value the user enters, carried forward by the purchases and sales recorded
in the portfolio.

FCast cannot see rewards, packs or SBCs, so the balance drifts from the game over time; entering
the current value again resets it. Positions are only counted if they were bought or sold after
the balance was entered - deleting a position therefore corrects the balance as well.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from fcast.analysis import signals as sig
from fcast.analysis.pricing import net_after_tax
from fcast.db import repositories as repo
from fcast.db.base import utcnow
from fcast.db.models import PositionStatus

KEY_COINS = "portfolio.coins"
KEY_SET_AT = "portfolio.coins_at"

BUY_RULES = frozenset({sig.Rule.BUY_DIP, sig.Rule.PROMO_PREBUY, sig.Rule.OVERPRICE_CHANCE})


def buy_price(signal: sig.Signal) -> int | None:
    """Coins needed to act on a signal; None if it is about a card you already own."""
    if signal.rule not in BUY_RULES:
        return None
    if signal.rule is sig.Rule.OVERPRICE_CHANCE and signal.expected_profit is None:
        return None  # extinct: only cards you already own can be listed
    return signal.price


@dataclass(frozen=True)
class CoinBalance:
    coins: int  # current balance after the recorded trades
    entered: int  # value the user entered
    entered_at: datetime
    spent: int  # purchases since then
    received: int  # sales since then, after the 5 % tax


def set_balance(session: Session, coins: int, now: datetime | None = None) -> None:
    if coins < 0:
        raise ValueError("coin balance must not be negative")
    repo.set_app_setting(session, KEY_COINS, str(coins))
    repo.set_app_setting(session, KEY_SET_AT, (now or utcnow()).isoformat())


def balance(session: Session) -> CoinBalance | None:
    """Current balance, or None if the user never entered one."""
    stored = repo.get_app_settings(session, "portfolio.")
    try:
        entered = int(stored[KEY_COINS])
        entered_at = datetime.fromisoformat(stored[KEY_SET_AT])
    except (KeyError, ValueError):
        return None
    spent = 0
    received = 0
    for position in repo.list_positions(session):
        if position.bought_at > entered_at and position.buy_price is not None:
            spent += position.buy_price
        if (
            position.status is PositionStatus.SOLD
            and position.sell_price is not None
            and position.sold_at is not None
            and position.sold_at > entered_at
        ):
            received += net_after_tax(position.sell_price)
    return CoinBalance(entered - spent + received, entered, entered_at, spent, received)


def affordable(price: int | None, coins: CoinBalance | None) -> bool:
    """True if a card at `price` can be bought; without a balance everything counts."""
    return coins is None or price is None or price <= coins.coins
