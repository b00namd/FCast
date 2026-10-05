from fcast.analysis.cards import CardValue
from fcast.analysis.playvalue import PlayValue
from fcast.analysis.pricing import net_after_tax
from fcast.analysis.uev import by_rating, uev_candidates


def _value(player_id: int, games: int | None, price: int | None) -> CardValue:
    play = PlayValue(score=70.0, group="Sturm", base=70.0, reasons=())
    return CardValue(
        player_id=player_id,
        play=play,
        meta=70.0,
        price=price,
        usage_rate=None,
        games=games,
        rating=84,
        base_card=True,
    )


def test_sorted_by_games_most_played_first() -> None:
    values = {
        1: _value(1, games=1_000, price=5_000),
        2: _value(2, games=9_000, price=5_000),
        3: _value(3, games=5_000, price=5_000),
    }

    result = uev_candidates(values, max_price=60_000, premium=500)

    assert [c.player_id for c in result] == [2, 3, 1]


def test_skips_cards_that_are_too_expensive() -> None:
    values = {1: _value(1, games=1_000, price=70_000), 2: _value(2, games=500, price=10_000)}

    result = uev_candidates(values, max_price=60_000, premium=500)

    assert [c.player_id for c in result] == [2]


def test_skips_cards_without_price_or_usage() -> None:
    values = {
        1: _value(1, games=None, price=5_000),
        2: _value(2, games=1_000, price=None),
        3: _value(3, games=1_000, price=5_000),
    }

    result = uev_candidates(values, max_price=60_000, premium=500)

    assert [c.player_id for c in result] == [3]


def test_break_even_covers_the_premium_after_tax() -> None:
    values = {1: _value(1, games=1_000, price=4_500)}

    candidate = uev_candidates(values, max_price=60_000, premium=500)[0]

    assert candidate.max_buy == 5_000
    assert net_after_tax(candidate.break_even) >= candidate.max_buy


def test_limit_keeps_the_most_played() -> None:
    values = {i: _value(i, games=i * 100, price=5_000) for i in range(1, 6)}

    result = uev_candidates(values, max_price=60_000, premium=500, limit=2)

    assert [c.player_id for c in result] == [5, 4]


def test_rating_filter() -> None:
    a = _value(1, games=1_000, price=5_000)
    b = CardValue(
        player_id=2,
        play=a.play,
        meta=70.0,
        price=5_000,
        usage_rate=None,
        games=2_000,
        rating=90,
        base_card=True,
    )
    values = {1: a, 2: b}

    result = uev_candidates(values, max_price=60_000, premium=500, rating=90)

    assert [c.player_id for c in result] == [2]


def test_by_rating_groups_best_rating_first() -> None:
    def card(player_id: int, games: int, rating: int | None) -> CardValue:
        base = _value(player_id, games=games, price=5_000)
        return CardValue(
            player_id=player_id,
            play=base.play,
            meta=70.0,
            price=5_000,
            usage_rate=None,
            games=games,
            rating=rating,
            base_card=True,
        )

    values = {
        1: card(1, 100, 86),
        2: card(2, 900, 86),
        3: card(3, 500, 90),
        4: card(4, 700, None),  # no rating: left out
    }

    groups = by_rating(uev_candidates(values, max_price=60_000, premium=500))

    assert [rating for rating, _ in groups] == [90, 86]
    assert [c.player_id for c in groups[1][1]] == [2, 1]


def test_by_rating_limit_applies_per_rating() -> None:
    values = {i: _value(i, games=i * 100, price=5_000) for i in range(1, 5)}

    groups = by_rating(uev_candidates(values, max_price=60_000, premium=500), limit=2)

    assert len(groups) == 1
    assert [c.player_id for c in groups[0][1]] == [4, 3]
