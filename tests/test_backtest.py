"""Backtesting on constructed price series whose outcome is known in advance."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.orm import Session, sessionmaker

from fcast.analysis.signals import Rule
from fcast.analysis.stats import Point
from fcast.backtest import engine
from fcast.backtest import service as bt
from fcast.backtest.curves import CardCurve, EventCurves, event_curve
from fcast.backtest.engine import CardSeries, Exit, Params, Trade
from fcast.config import Platform, Settings
from fcast.db import repositories as repo
from fcast.db.base import Base
from fcast.db.models import LinkType
from fcast.db.session import create_db_engine, create_session_factory
from fcast.promos.scoring import PromoInfo, PromoWeights

T0 = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)
WEEK_H = 7 * 24
DEFAULT = Params()


def hourly(*runs: tuple[int, int], start: datetime = T0) -> tuple[Point, ...]:
    """Hourly points from (count, price) runs, e.g. (168, 10_000), (1, 8_500)."""
    points: list[Point] = []
    for count, price in runs:
        for _ in range(count):
            points.append((start + len(points) * HOUR, price))
    return tuple(points)


def card(points: tuple[Point, ...], ea_id: int = 1, club: str | None = None) -> CardSeries:
    return CardSeries(ea_id=ea_id, name=f"Karte {ea_id}", points=points, club=club)


def run_dip(series: list[CardSeries], params: Params = DEFAULT) -> list[Trade]:
    start, end = series[0].points[0][0], max(s.points[-1][0] for s in series)
    return engine.simulate_dip(series, engine.dip_stats(series, start, end), params, end)


# --- BUY_DIP ----------------------------------------------------------------------------


def test_dip_that_recovers_sells_at_the_mean() -> None:
    # One week at 10,000, a dip to 8,500, then back to 10,000.
    trades = run_dip([card(hourly((WEEK_H, 10_000), (1, 8_500), (5, 10_000)))])
    assert len(trades) == 1
    trade = trades[0]
    assert trade.buy == 8_500
    assert trade.bought_at == T0 + WEEK_H * HOUR
    # 7-day mean (168 x 10,000 + 8,500) = 9,991 -> sell target 9,900 (valid price step).
    assert trade.sell == 9_900
    assert trade.exit is Exit.TARGET
    assert trade.sold_at == T0 + (WEEK_H + 1) * HOUR
    assert trade.profit == 9_405 - 8_500  # 5 % tax
    m = engine.metrics(trades)
    assert (m.trades, m.wins, m.hit_rate, m.total_profit) == (1, 1, 100.0, 905)
    assert m.max_drawdown == 0
    assert m.peak_capital == 8_500
    assert m.avg_hold_h == 1.0


def test_dip_without_recovery_is_sold_after_the_holding_period() -> None:
    trades = run_dip([card(hourly((WEEK_H, 10_000), (100, 8_500)))], Params(max_hold_h=72))
    assert len(trades) == 1  # later the mean has come down: no new dip
    trade = trades[0]
    assert trade.exit is Exit.HOLD
    assert trade.held_h == 72
    assert trade.sell == 8_400  # one step below the market
    assert trade.profit == 7_980 - 8_500
    m = engine.metrics(trades)
    assert (m.wins, m.hit_rate, m.max_drawdown) == (0, 0.0, 520)


def test_stop_loss_sells_early() -> None:
    points = hourly((WEEK_H, 10_000), (10, 8_500), (50, 7_500))
    trade = run_dip([card(points)], Params(stop_loss_pct=5))[0]
    assert trade.exit is Exit.STOP
    assert trade.sold_at == T0 + (WEEK_H + 10) * HOUR
    assert trade.sell == 7_400


def test_position_open_at_the_end_is_valued_at_the_market() -> None:
    trades = run_dip([card(hourly((WEEK_H, 10_000), (10, 8_500)))])
    assert [t.exit for t in trades] == [Exit.OPEN]
    m = engine.metrics(trades)
    assert (m.trades, m.open, m.hit_rate, m.total_profit) == (0, 1, None, 0)
    assert m.unrealized == 7_980 - 8_500
    assert m.peak_capital == 8_500  # still tied up


def test_no_lookahead_signal_only_sees_the_past() -> None:
    # The dip is at the very start of the data: no 7-day history, so no signal.
    trades = run_dip([card(hourly((1, 8_500), (WEEK_H, 10_000)))])
    assert trades == []


def test_sweep_over_dip_threshold() -> None:
    series = [card(hourly((WEEK_H, 10_000), (1, 8_500), (5, 10_000)))]  # a 15 % dip
    start, end = T0, series[0].points[-1][0]
    cache = engine.dip_stats(series, start, end)
    rows = engine.sweep(
        lambda p: engine.simulate_dip(series, cache, p, end), Params(), "dip_pct", [5, 10, 20]
    )
    assert [(r.value, r.metrics.trades) for r in rows] == [(5, 1), (10, 1), (20, 0)]


def test_metrics_drawdown_and_capital() -> None:
    def trade(start_h: int, end_h: int, buy: int, sell: int) -> Trade:
        return Trade(1, "X", T0 + start_h * HOUR, buy, T0 + end_h * HOUR, sell, Exit.TARGET)

    trades = [
        trade(0, 2, 10_000, 11_579),  # net floor(11,579 * 0.95) = 11,000 -> +1,000
        trade(1, 3, 10_000, 7_395),  # net 7,025 -> -2,975
        trade(4, 5, 1_000, 1_500),  # net 1,425 -> +425
        trade(4, 6, 5_000, 4_158),  # net 3,950 -> -1,050
    ]
    m = engine.metrics(trades)
    assert [t.profit for t in trades] == [1_000, -2_975, 425, -1_050]
    # Equity 1,000 -> -1,975 -> -1,550 -> -2,600: peak 1,000, trough -2,600.
    assert m.max_drawdown == 3_600
    assert m.peak_capital == 20_000  # the first two overlap
    assert m.total_profit == -2_600
    assert m.hit_rate == 50.0


# --- PROMO_PREBUY -------------------------------------------------------------------------

PROMO_START = T0 + 240 * HOUR


def promo() -> PromoInfo:
    return PromoInfo(
        promo_id=1,
        name="Road to the Knockouts",
        starts_at=PROMO_START,
        ends_at=None,
        confidence=0.9,
        links=(("club", "Arsenal"),),
    )


def promo_series() -> list[CardSeries]:
    # Liquid market (price moves every hour) until 12 h before the start, then +25 %.
    wobble = [(1, 20_000 if i % 2 == 0 else 20_250) for i in range(228)]
    rising = hourly(*wobble, (60, 25_000))
    return [
        card(rising, ea_id=1, club="Arsenal"),
        card(rising, ea_id=2, club="Chelsea"),  # not linked
    ]


def run_prebuy(params: Params = DEFAULT) -> list[Trade]:
    series = promo_series()
    return engine.simulate_prebuy(
        series, [promo()], params, PromoWeights(), T0, series[0].points[-1][0]
    )


def test_prebuy_buys_before_the_promo_and_sells_after_the_start() -> None:
    trades = run_prebuy(Params(entry_days=3, exit_h=24))
    assert [t.ea_id for t in trades] == [1]
    trade = trades[0]
    assert trade.bought_at == PROMO_START - timedelta(days=3)
    assert trade.buy == 20_000  # hour 168 is even
    assert trade.sold_at == PROMO_START + timedelta(hours=24)
    assert trade.sell == 24_750
    assert trade.exit is Exit.EVENT
    assert trade.profit == 23_512 - 20_000
    assert "Road to the Knockouts" in trade.note


def test_prebuy_threshold_and_exit_sweep() -> None:
    assert run_prebuy(Params(threshold=90)) == []  # score is about 55
    early = run_prebuy(Params(exit_h=-24))[0]  # sold before the price moved
    assert early.sell == 19_750
    assert early.profit == 18_762 - 20_000


def test_prebuy_needs_a_fresh_price() -> None:
    series = [card(hourly((10, 20_000)), club="Arsenal")]  # data ends 9 days before the start
    trades = engine.simulate_prebuy(
        series, [promo()], Params(), PromoWeights(), T0, PROMO_START + timedelta(days=2)
    )
    assert trades == []


# --- curves -------------------------------------------------------------------------------


def test_event_curve_relative_to_three_days_before() -> None:
    event = T0 + 100 * HOUR
    # 10,000 until just before T-24h, then 11,000, from T+1h on 12,000.
    points = hourly((76, 10_000), (25, 11_000), (60, 12_000))
    curve = event_curve(points, event)
    assert curve == {-72: 0.0, -48: 0.0, -24: 10.0, 0: 10.0, 24: 20.0, 48: 20.0}
    assert event_curve(points[:3], event) is None  # no fresh price three days before

    group = EventCurves("promo", "Test", event, "verknüpft")
    group.cards = [
        CardCurve(1, "A", curve),
        CardCurve(2, "B", {-72: 0.0, 24: -10.0}),
    ]
    assert group.average()[24] == (5.0, 2)
    assert group.average()[-24] == (10.0, 1)


# --- service: reproducibility, windows, input ---------------------------------------------


@pytest.fixture
def factory() -> sessionmaker[Session]:
    engine_ = create_db_engine("sqlite://")
    Base.metadata.create_all(engine_)
    return create_session_factory(engine_)


def settings() -> Settings:
    return Settings(_env_file=None, platform="pc", sources="")


def store(session: Session, ea_id: int, points: tuple[Point, ...], club: str | None = None) -> None:
    player = repo.upsert_player(session, ea_id, repo.PlayerDetails(name=f"P{ea_id}", club=club))
    for at, price in points:
        repo.add_snapshot(session, player, Platform.PC, price, "futbin", at)


def test_backtest_report_is_reproducible(factory: sessionmaker[Session]) -> None:
    start, end = T0 + WEEK_H * HOUR, T0 + 400 * HOUR
    with factory.begin() as session:
        store(session, 11, hourly((WEEK_H, 10_000), (1, 8_500), (5, 10_000)))
        store(session, 12, hourly((WEEK_H, 50_000), (30, 50_500)))
        first = bt.run_backtest(session, settings(), Rule.BUY_DIP, start, end)
        again = bt.run_backtest(session, settings(), Rule.BUY_DIP, start, end)
    assert first.trades == again.trades
    assert first.metrics == again.metrics
    assert first.fingerprint == again.fingerprint
    assert (first.cards, first.metrics.trades, first.metrics.total_profit) == (2, 1, 905)

    with factory.begin() as session:  # new data inside the window -> new fingerprint
        store(session, 13, hourly((3, 1_000), start=start))
        changed = bt.run_backtest(session, settings(), Rule.BUY_DIP, start, end)
    assert changed.fingerprint != first.fingerprint

    with factory.begin() as session:  # data after the window does not change the report
        store(session, 14, hourly((3, 1_000), start=end + HOUR))
        later = bt.run_backtest(session, settings(), Rule.BUY_DIP, start, end)
    assert later.fingerprint == changed.fingerprint


def test_backtest_sweep_and_prebuy_from_database(factory: sessionmaker[Session]) -> None:
    with factory.begin() as session:
        store(session, 11, hourly((WEEK_H, 10_000), (1, 8_500), (5, 10_000)))
        swept = bt.run_backtest(
            session,
            settings(),
            Rule.BUY_DIP,
            T0,
            T0 + 300 * HOUR,
            sweep_name="dip_pct",
            sweep_values=[10, 20],
        )
    assert [r.metrics.trades for r in swept.sweep] == [1, 0]
    assert swept.best_sweep is not None and swept.best_sweep.value == 10

    with factory.begin() as session:
        for ea_id, club in ((1, "Arsenal"), (2, "Chelsea")):
            store(session, ea_id, promo_series()[0].points, club=club)
        created = repo.create_promo(session, "Road to the Knockouts", PROMO_START, confidence=0.9)
        repo.add_promo_link(session, created, LinkType.CLUB, "Arsenal")
        report = bt.run_backtest(
            session, settings(), Rule.PROMO_PREBUY, T0, T0 + 300 * HOUR, Params(exit_h=24)
        )
        curves = bt.promo_curves(session, settings(), T0, T0 + 300 * HOUR)
    assert report.promos == 1
    assert [t.ea_id for t in report.trades] == [1]
    assert report.metrics.total_profit == 3_512
    assert len(curves) == 1
    assert [c.ea_id for c in curves[0].cards] == [1]
    change, cards = curves[0].average()[24]
    assert (round(change, 1), cards) == (25.0, 1)


def test_unsupported_rules_and_bad_sweeps(factory: sessionmaker[Session]) -> None:
    with factory() as session, pytest.raises(ValueError):
        bt.run_backtest(session, settings(), Rule.HOLO_SPREAD, T0, T0 + HOUR)
    assert bt.parse_sweep(Rule.BUY_DIP, "dip_pct=5, 10;15") == ("dip_pct", [5.0, 10.0, 15.0])
    assert bt.parse_sweep(Rule.BUY_DIP, "max_hold_h=24,48.5")[1] == [24.0, 48.5]
    with pytest.raises(ValueError, match="gibt es"):
        bt.parse_sweep(Rule.BUY_DIP, "threshold=40,50")
    with pytest.raises(ValueError, match="keine Zahlen"):
        bt.parse_sweep(Rule.BUY_DIP, "dip_pct=a,b")


def test_window_uses_local_days() -> None:
    tz = ZoneInfo("Europe/Berlin")
    now = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    start, end = bt.window(date(2026, 9, 1), date(2026, 9, 30), tz, now)
    assert start == datetime(2026, 8, 31, 22, 0, tzinfo=UTC)
    assert end == datetime(2026, 9, 30, 22, 0, tzinfo=UTC) - timedelta(microseconds=1)
    start, end = bt.window(None, None, tz, now)
    assert end == now  # today is cut at now
    assert start == datetime(2026, 8, 31, 22, 0, tzinfo=UTC)
    with pytest.raises(ValueError):
        bt.window(date(2026, 9, 30), date(2026, 9, 1), tz, now)
