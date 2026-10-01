"""Statistics and signals on synthetic price series (dip, spike, flat, gaps, outliers, extinct)."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from fcast.analysis import signals as sig
from fcast.analysis.market import effective_price, headroom_pct, supply_gap_pct
from fcast.analysis.pricing import is_valid_price, net_after_tax
from fcast.analysis.stats import (
    Point,
    changes_per_day,
    hour_profile,
    price_stats,
    weekday_profile,
)

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
CFG = sig.SignalConfig()


def series(prices: list[int], step: timedelta = timedelta(hours=1)) -> list[Point]:
    """Prices ending at NOW, one point per `step`."""
    return [(NOW - step * (len(prices) - 1 - i), p) for i, p in enumerate(prices)]


def flat(price: int, hours: int = 7 * 24) -> list[Point]:
    return series([price] * hours)


# --- market ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("listings", "expected"),
    [
        ((), None),
        ((10_000,), 10_000),
        ((10_000, 10_500), 10_000),
        ((8_000, 10_000), 10_000),  # 20 % below the second: outlier
        ((8_600, 10_000), 8_600),  # 14 %: still a real price
        ((5_500_000, 8_150_000), 8_150_000),  # the real FUTBIN case from Phase 2
    ],
)
def test_effective_price(listings: tuple[int, ...], expected: int | None) -> None:
    assert effective_price(listings, 15.0) == expected


def test_supply_gap_and_headroom() -> None:
    assert supply_gap_pct((10_000, 12_000)) == pytest.approx(20.0)
    assert supply_gap_pct((10_000,)) is None
    assert headroom_pct(10_000, 15_000) == pytest.approx(50.0)
    assert headroom_pct(15_000, 15_000) == 0.0
    assert headroom_pct(10_000, None) is None


# --- statistics ------------------------------------------------------------


def test_flat_series() -> None:
    stats = price_stats(flat(10_000), NOW)
    assert stats.current == 10_000
    assert stats.week is not None
    assert stats.week.mean == 10_000
    assert stats.week.stdev == 0
    assert stats.change_24h_pct == 0
    assert stats.deviation_pct == 0
    assert stats.volatility_pct == 0
    assert stats.changes_per_day == 0


def test_dip_series() -> None:
    stats = price_stats(series([10_000] * 160 + [8_000]), NOW)
    assert stats.current == 8_000
    assert stats.deviation_pct == pytest.approx(-19.9, abs=0.1)
    assert stats.change_24h_pct == pytest.approx(-20.0)
    assert stats.day is not None
    assert stats.day.low == 8_000


def test_spike_series() -> None:
    stats = price_stats(series([10_000] * 160 + [15_000]), NOW)
    assert stats.change_24h_pct == pytest.approx(50.0)
    assert stats.volatility_pct is not None
    assert stats.volatility_pct > 0


def test_gaps_in_data() -> None:
    # Two days of data, a three-day gap, then one more day.
    points = series([10_000] * 48, timedelta(hours=1))
    points = [(at - timedelta(days=4), p) for at, p in points] + series([12_000] * 24)
    stats = price_stats(points, NOW)
    assert stats.current == 12_000
    assert stats.week is not None
    assert stats.week.count == 72
    # The price 24 h ago is the last one before the window, not interpolated.
    assert stats.change_24h_pct == pytest.approx(20.0)


def test_empty_and_tiny_series() -> None:
    empty = price_stats([], NOW)
    assert empty.current is None
    assert empty.week is None
    assert empty.change_24h_pct is None
    one = price_stats([(NOW, 5_000)], NOW)
    assert one.current == 5_000
    assert one.volatility_pct is None
    assert one.changes_per_day is None
    assert one.change_24h_pct is None


def test_old_data_only() -> None:
    stats = price_stats(series([10_000] * 5, timedelta(days=3)), NOW - timedelta(days=20))
    assert stats.current == 10_000


def test_changes_per_day() -> None:
    alternating = series([100, 200] * 24)  # 48 points over 47 h, every step changes
    assert changes_per_day(alternating, NOW - timedelta(days=7)) == pytest.approx(24, rel=0.03)


def test_profiles_in_local_time() -> None:
    berlin = ZoneInfo("Europe/Berlin")
    # 20:00 UTC == 22:00 Berlin (summer time): make that hour expensive.
    points = [
        (datetime(2026, 9, day, hour, tzinfo=UTC), 12_000 if hour == 20 else 10_000)
        for day in range(21, 28)
        for hour in range(24)
    ]
    hours = hour_profile(points, berlin)
    assert max(hours, key=lambda h: hours[h]) == 22
    assert hours[22] > 15
    days = weekday_profile(points, berlin)
    assert set(days) == set(range(7))
    assert hour_profile([], berlin) == {}


# --- signals ---------------------------------------------------------------


def test_buy_dip_signal() -> None:
    stats = price_stats(series([10_000] * 160 + [8_000]), NOW)
    signal = sig.buy_dip(1, "X", stats, CFG)
    assert signal is not None
    assert signal.rule is sig.Rule.BUY_DIP
    assert signal.recommended is not None
    assert is_valid_price(signal.recommended)
    # Buying at the recommended max still leaves the margin when selling at the mean.
    assert net_after_tax(9_900) - signal.recommended >= CFG.min_profit
    assert signal.expected_profit == net_after_tax(9_900) - 8_000


def test_no_buy_dip_for_a_fresh_card_falling_after_release() -> None:
    # A new TOTW card: 229k at release, then falling every half hour. The "mean" of the first
    # hours is just the launch price, not a reference for a dip.
    launch = [229_000, 200_000, 180_000, 160_000, 140_000, 130_000, 120_000, 110_000, 100_000]
    stats = price_stats(series(launch, timedelta(minutes=30)), NOW)
    assert stats.week is not None and stats.week.count >= sig.MIN_POINTS_DIP
    assert sig.buy_dip(1, "Son", stats, CFG) is None
    # Same shape after three days of history is a real dip again.
    history = series([120_000] * 72 + [100_000])
    assert sig.buy_dip(1, "Son", price_stats(history, NOW), CFG) is not None


def test_no_buy_dip_without_margin_after_tax() -> None:
    # 12 % below the mean (a real dip), but selling at the mean only earns ~20 coins after tax.
    stats = price_stats(series([1_000] * 160 + [880]), NOW)
    assert stats.deviation_pct is not None
    assert stats.deviation_pct < -CFG.dip_pct
    assert sig.buy_dip(1, "X", stats, CFG) is None
    # The same dip passes with no minimum profit.
    lenient = sig.SignalConfig(min_profit=0, min_margin_pct=0)
    assert sig.buy_dip(1, "X", stats, lenient) is not None


def test_no_buy_dip_on_flat_or_thin_data() -> None:
    assert sig.buy_dip(1, "X", price_stats(flat(10_000), NOW), CFG) is None
    thin = price_stats(series([10_000, 10_000, 7_000]), NOW)
    assert sig.buy_dip(1, "X", thin, CFG) is None


def test_sell_target_signal() -> None:
    stats = price_stats(flat(12_000), NOW)
    assert sig.sell_target(1, "X", stats, 11_000) is not None
    assert sig.sell_target(1, "X", stats, 12_500) is None
    assert sig.sell_target(1, "X", stats, None) is None


def rising(end: int) -> list[Point]:
    start = end * 0.85
    prices = [round(start + (end - start) * i / 167) for i in range(168)]
    return series(prices)


def test_overprice_chance_with_thin_supply() -> None:
    stats = price_stats(rising(100_000), NOW)
    supply = sig.Supply(listings=(100_000, 130_000), range_max=300_000)
    signal = sig.overprice_chance(1, "X", stats, supply, CFG)
    assert signal is not None
    assert signal.score is not None
    assert signal.score >= CFG.uev_threshold
    # List just below the next listing, on a valid step.
    assert signal.recommended == 129_000
    assert signal.expected_profit == net_after_tax(129_000) - 100_000
    assert any("Lücke" in reason for reason in signal.reasons)


def test_no_overprice_chance_for_a_tiny_profit() -> None:
    # Thin supply and rising, but the next listing is only two steps up: after tax a loss-ish
    # few coins, below the minimum profit (like Khusanov: +12 coins at 28,250 -> 29,750).
    stats = price_stats(rising(28_250), NOW)
    supply = sig.Supply(listings=(28_250, 30_000), range_max=60_000)
    score = sig.overprice_score(stats, supply, CFG)
    assert score is not None and score.score >= CFG.uev_threshold
    assert sig.overprice_chance(1, "X", stats, supply, CFG) is None


def test_no_overprice_chance_with_deep_supply() -> None:
    stats = price_stats(flat(100_000), NOW)
    supply = sig.Supply(listings=(100_000, 100_000, 101_000, 101_000, 102_000), range_max=300_000)
    score = sig.overprice_score(stats, supply, CFG)
    assert score is not None
    assert score.score < CFG.uev_threshold
    assert sig.overprice_chance(1, "X", stats, supply, CFG) is None


def test_overprice_for_extinct_card() -> None:
    stats = price_stats(rising(50_000), NOW)
    supply = sig.Supply(listings=(), range_max=200_000, extinct_since=NOW)
    signal = sig.overprice_chance(1, "X", stats, supply, CFG)
    assert signal is not None
    assert signal.expected_profit is None  # cannot be bought, only sold if owned
    assert signal.recommended == 60_000  # +20 % on 50.000
    assert "extinct" in " ".join(signal.reasons)


def test_overprice_listing_is_capped_by_ea_maximum() -> None:
    stats = price_stats(rising(100_000), NOW)
    supply = sig.Supply(listings=(100_000, 150_000), range_max=110_000)
    assert sig.overprice_listing_price(stats, supply, CFG) == 110_000


def test_overprice_score_is_deterministic_and_weighted() -> None:
    stats = price_stats(rising(100_000), NOW)
    supply = sig.Supply(listings=(100_000, 130_000), range_max=300_000)
    first = sig.overprice_score(stats, supply, CFG)
    assert first == sig.overprice_score(stats, supply, CFG)
    supply_only = sig.SignalConfig(
        uev_weight_supply=1, uev_weight_trend=0, uev_weight_headroom=0, uev_weight_liquidity=0
    )
    score = sig.overprice_score(stats, supply, supply_only)
    assert score is not None
    assert score.score == 100.0  # 30 % gap = full supply score


def test_signal_config_from_settings() -> None:
    from fcast.config import Settings

    settings = Settings(_env_file=None, dip_pct=12.5, uev_threshold=70)
    cfg = sig.SignalConfig.from_settings(settings)
    assert cfg.dip_pct == 12.5
    assert cfg.uev_threshold == 70


def test_thin_since_finds_the_current_run_of_thin_supply() -> None:
    h = timedelta(hours=1)
    assert sig.thin_since([]) is None
    assert sig.thin_since([(NOW - 2 * h, 2), (NOW - h, 5), (NOW, 5)]) is None  # full now
    history = [(NOW - 3 * h, 2), (NOW - 2 * h, 5), (NOW - h, 3), (NOW, 0)]
    assert sig.thin_since(history) == NOW - h  # the break at -2 h ends the earlier run


def test_persistent_thin_supply_raises_the_uev_score() -> None:
    stats = price_stats(rising(100_000), NOW)
    listings = (100_000, 104_000, 105_000)
    fresh = sig.overprice_score(stats, sig.Supply(listings, 300_000), CFG)
    lasting = sig.overprice_score(stats, sig.Supply(listings, 300_000, thin_hours=48), CFG)
    assert fresh is not None and lasting is not None
    assert lasting.score > fresh.score
    assert "Angebot seit 48 h dünn" in lasting.reasons
    short = sig.overprice_score(stats, sig.Supply(listings, 300_000, thin_hours=2), CFG)
    assert short is not None and short.score == fresh.score


def test_supply_without_outlier_listing() -> None:
    supply = sig.Supply.observed((5_000, 9_800, 9_900, 10_000), 50_000, 15.0)
    assert supply.listings == (9_800, 9_900, 10_000)
