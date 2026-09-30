"""Transfer market arithmetic: price steps and EA tax.

Price steps (bid increments): up to 1,000 in 50s, up to 10,000 in 100s, up to 50,000 in 250s,
up to 100,000 in 500s, above that in 1,000s. A boundary value (e.g. 1,000) is valid in both
neighbouring tiers; moving up from it uses the higher tier's step.
"""

import math
from typing import Literal

EA_TAX = 0.05
MIN_PRICE = 150

# (upper bound of the tier, step inside the tier)
_TIERS: tuple[tuple[float, int], ...] = (
    (1_000, 50),
    (10_000, 100),
    (50_000, 250),
    (100_000, 500),
    (math.inf, 1_000),
)

Rounding = Literal["nearest", "down", "up"]


def price_step(price: float) -> int:
    """Step size that applies above `price` (at a boundary the higher tier's step)."""
    for upper, step in _TIERS:
        if price < upper:
            return step
    raise AssertionError("unreachable")  # pragma: no cover


def _step_below(price: float) -> int:
    """Step size that applies just below `price`."""
    for upper, step in _TIERS:
        if price <= upper:
            return step
    raise AssertionError("unreachable")  # pragma: no cover


def is_valid_price(price: int) -> bool:
    return price >= MIN_PRICE and price % price_step(price) == 0


def round_to_price_step(price: float, mode: Rounding = "nearest") -> int:
    """Round to a valid market price (never below MIN_PRICE)."""
    if price <= MIN_PRICE:
        return MIN_PRICE
    step = price_step(price)
    lower = math.floor(price / step) * step
    upper = lower + step
    # The upper candidate may cross into the next tier; it is still a valid price there.
    if mode == "down":
        result = lower
    elif mode == "up":
        result = lower if lower == price else upper
    else:
        result = lower if price - lower < upper - price else upper
    return max(MIN_PRICE, int(result))


def step_price(price: int, steps: int) -> int:
    """Move a valid price `steps` increments up (positive) or down (negative)."""
    current = round_to_price_step(price, "nearest")
    for _ in range(abs(steps)):
        if steps > 0:
            current += price_step(current)
        else:
            current = max(MIN_PRICE, current - _step_below(current))
    return current


def net_after_tax(price: int) -> int:
    """Coins received for a sale at `price` after the 5 % EA tax (EA rounds down)."""
    return math.floor(price * (1 - EA_TAX))


def profit(buy_price: int, sell_price: int) -> int:
    return net_after_tax(sell_price) - buy_price


def break_even_sell_price(buy_price: int) -> int:
    """Lowest valid listing price whose after-tax proceeds cover `buy_price`."""
    price = round_to_price_step(buy_price / (1 - EA_TAX), "up")
    while net_after_tax(price) < buy_price:
        price = step_price(price, 1)
    return price
