from fcast.analysis import cards
from fcast.analysis import playvalue as pv
from fcast.analysis.pricing import net_after_tax
from fcast.analysis.uev import by_rating, uev_candidates


def _value(pid: int, price: int | None, games: int | None, rating: int | None) -> cards.CardValue:
    play = pv.PlayValue(70.0, pv.STRIKER, 80.0, ())
    return cards.CardValue(pid, play, 70.0, price, None, games, rating, True)


def _values(*items: cards.CardValue) -> dict[int, cards.CardValue]:
    return {v.player_id: v for v in items}


def test_candidates_need_price_and_games_and_respect_max_price() -> None:
    values = _values(
        _value(1, 10_000, 500, 86),
        _value(2, None, 900, 86),  # no recent price
        _value(3, 12_000, None, 86),  # no usage data
        _value(4, 70_000, 2_000, 88),  # too expensive
        _value(5, 3_000, 1_500, 84),
    )
    found = uev_candidates(values, max_price=60_000, premium=500)
    assert [c.player_id for c in found] == [5, 1]  # most played first


def test_max_buy_and_break_even_use_valid_price_steps() -> None:
    (candidate,) = uev_candidates(_values(_value(1, 9_800, 10, 85)), max_price=60_000, premium=500)
    assert candidate.max_buy == 10_250  # 10,300 rounded down to the 250 step
    assert candidate.break_even == 11_000
    assert net_after_tax(candidate.break_even) >= candidate.max_buy
    assert net_after_tax(10_750) < candidate.max_buy


def test_rating_filter_and_limit() -> None:
    values = _values(*(_value(i, 5_000, 100 * i, 86 if i % 2 else 87) for i in range(1, 7)))
    found = uev_candidates(values, max_price=60_000, premium=0, rating=86, limit=2)
    assert [c.player_id for c in found] == [5, 3]
    assert all(c.max_buy == 5_000 for c in found)


def test_by_rating_groups_highest_first_and_limits_each_group() -> None:
    values = _values(
        _value(1, 5_000, 100, 85),
        _value(2, 5_000, 300, 85),
        _value(3, 5_000, 200, 85),
        _value(4, 8_000, 50, 87),
        _value(5, 1_000, 999, None),
    )
    groups = by_rating(uev_candidates(values, max_price=60_000, premium=500), limit=2)
    assert [(r, [c.player_id for c in g]) for r, g in groups] == [
        (87, [4]),
        (85, [2, 3]),
        (None, [5]),
    ]
