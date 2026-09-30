import pytest

from fcast.analysis.pricing import (
    MIN_PRICE,
    break_even_sell_price,
    is_valid_price,
    net_after_tax,
    price_step,
    profit,
    round_to_price_step,
    step_price,
)


@pytest.mark.parametrize(
    ("price", "step"),
    [
        (150, 50),
        (999, 50),
        (1_000, 100),  # boundary belongs to the higher tier when moving up
        (9_999, 100),
        (10_000, 250),
        (49_999, 250),
        (50_000, 500),
        (99_999, 500),
        (100_000, 1_000),
        (15_000_000, 1_000),
    ],
)
def test_price_step(price: int, step: int) -> None:
    assert price_step(price) == step


@pytest.mark.parametrize(
    ("price", "valid"),
    [
        (150, True),
        (100, False),
        (950, True),
        (975, False),
        (1_000, True),
        (1_050, False),
        (1_100, True),
        (10_000, True),
        (10_250, True),
        (10_100, False),
        (50_500, True),
        (50_250, False),
        (100_000, True),
        (100_500, False),
        (101_000, True),
    ],
)
def test_is_valid_price(price: int, valid: bool) -> None:
    assert is_valid_price(price) is valid


@pytest.mark.parametrize(
    ("price", "mode", "expected"),
    [
        (1_049, "nearest", 1_000),
        (1_050, "nearest", 1_100),  # tie rounds up
        (1_049, "down", 1_000),
        (1_001, "up", 1_100),
        (1_000, "up", 1_000),
        (976, "nearest", 1_000),
        (9_990, "down", 9_900),
        (9_990, "up", 10_000),
        (10_100, "nearest", 10_000),
        (10_130, "nearest", 10_250),
        (99_800, "up", 100_000),
        (123_456, "down", 123_000),
        (123_456, "up", 124_000),
        (123_456.7, "nearest", 123_000),
        (50, "down", MIN_PRICE),
        (0, "nearest", MIN_PRICE),
    ],
)
def test_round_to_price_step(price: float, mode: str, expected: int) -> None:
    assert round_to_price_step(price, mode) == expected  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("price", "steps", "expected"),
    [
        (950, 1, 1_000),
        (1_000, 1, 1_100),
        (1_000, -1, 950),
        (10_000, -1, 9_900),
        (10_000, 1, 10_250),
        (100_000, -1, 99_500),
        (100_000, 2, 102_000),
        (200, -5, MIN_PRICE),
        (5_000, 0, 5_000),
    ],
)
def test_step_price(price: int, steps: int, expected: int) -> None:
    assert step_price(price, steps) == expected


def test_tax_and_profit() -> None:
    assert net_after_tax(10_000) == 9_500
    assert net_after_tax(1_050) == 997  # EA rounds down
    assert profit(10_000, 12_000) == 1_400
    assert profit(10_000, 10_000) == -500


@pytest.mark.parametrize("buy", [200, 950, 1_000, 9_500, 10_000, 47_000, 99_000, 1_234_000])
def test_break_even_sell_price(buy: int) -> None:
    sell = break_even_sell_price(buy)
    assert is_valid_price(sell)
    assert net_after_tax(sell) >= buy
    assert net_after_tax(step_price(sell, -1)) < buy
