"""Portfolio overview shared by CLI and dashboard: profit after tax, market value, relists."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo

from sqlalchemy.orm import Session

from fcast.analysis.pricing import break_even_sell_price, net_after_tax, profit
from fcast.config import Settings
from fcast.db import repositories as repo
from fcast.db.models import PortfolioPosition, PositionStatus

OPEN_POSITIONS = (PositionStatus.HOLDING, PositionStatus.LISTED)
RELIST_HINT_AFTER = 2  # listings without a sale before FCast suggests a lower price


@dataclass(frozen=True)
class PositionRow:
    position: PortfolioPosition
    break_even: int | None  # None if the buy price is unknown
    profit: int | None  # realised profit after tax (sold positions with a known buy price)
    market: int | None  # current market price (open positions)
    at_market: int | None  # profit if sold at the market price now
    listings: int  # how often the card was put on the market
    market_below_listing: bool  # listed several times and the market is cheaper

    @property
    def unknown_buy(self) -> bool:
        return self.position.buy_price is None


@dataclass
class Summary:
    rows: list[PositionRow] = field(default_factory=list)
    realised: int = 0
    tied_up: int = 0  # known buy prices of open positions
    unrealised: int = 0  # sum of `at_market`


def position_row(session: Session, settings: Settings, position: PortfolioPosition) -> PositionRow:
    buy = position.buy_price
    gain = market = at_market = None
    if position.status is PositionStatus.SOLD:
        if buy is not None and position.sell_price is not None:
            gain = profit(buy, position.sell_price)
    else:
        snapshot = repo.latest_snapshot(session, position.player, settings.platform)
        if snapshot is not None and snapshot.price is not None:
            market = snapshot.price
            if buy is not None:
                at_market = profit(buy, market)
    listings = len(position.listings)
    return PositionRow(
        position=position,
        break_even=break_even_sell_price(buy) if buy is not None else None,
        profit=gain,
        market=market,
        at_market=at_market,
        listings=listings,
        market_below_listing=(
            position.status is PositionStatus.LISTED
            and listings >= RELIST_HINT_AFTER
            and market is not None
            and position.sell_price is not None
            and market < position.sell_price
        ),
    )


def summarize(
    session: Session, settings: Settings, statuses: Sequence[PositionStatus] | None
) -> Summary:
    summary = Summary()
    for position in repo.list_positions(session, statuses):
        row = position_row(session, settings, position)
        summary.rows.append(row)
        if row.profit is not None:
            summary.realised += row.profit
        if position.status is not PositionStatus.SOLD and position.buy_price is not None:
            summary.tied_up += position.buy_price
        if row.at_market is not None:
            summary.unrealised += row.at_market
    return summary


# --- profit per day and week ---------------------------------------------------------

DAYS_SHOWN = 14
WEEKS_SHOWN = 8


@dataclass
class PeriodProfit:
    start: date  # local day, or the Monday of the week
    profit: int = 0  # after the 5 % tax, sales with a known buy price only
    sales: int = 0  # sales counted in `profit`
    unknown: int = 0  # sales without a recorded purchase (not in `profit`)


@dataclass
class ProfitReport:
    today: PeriodProfit
    week: PeriodProfit
    total: PeriodProfit
    days: list[PeriodProfit]  # newest first, only days with sales
    weeks: list[PeriodProfit]  # newest first, only weeks with sales


def _add(period: PeriodProfit, gain: int | None) -> None:
    if gain is None:
        period.unknown += 1
    else:
        period.profit += gain
        period.sales += 1


def profit_report(session: Session, tz: tzinfo, now: datetime) -> ProfitReport:
    """Realised profit per local day and week (Monday to Sunday) and in total."""
    today = now.astimezone(tz).date()
    monday = today - timedelta(days=today.weekday())
    days: dict[date, PeriodProfit] = {}
    weeks: dict[date, PeriodProfit] = {}
    total = PeriodProfit(date.min)
    for position in repo.list_positions(session, [PositionStatus.SOLD]):
        if position.sold_at is None or position.sell_price is None:
            continue
        gain = (
            profit(position.buy_price, position.sell_price)
            if position.buy_price is not None
            else None
        )
        day = position.sold_at.astimezone(tz).date()
        week = day - timedelta(days=day.weekday())
        _add(days.setdefault(day, PeriodProfit(day)), gain)
        _add(weeks.setdefault(week, PeriodProfit(week)), gain)
        _add(total, gain)
    return ProfitReport(
        today=days.get(today, PeriodProfit(today)),
        week=weeks.get(monday, PeriodProfit(monday)),
        total=total,
        days=[days[d] for d in sorted(days, reverse=True)[:DAYS_SHOWN]],
        weeks=[weeks[w] for w in sorted(weeks, reverse=True)[:WEEKS_SHOWN]],
    )


# --- capital tied up -----------------------------------------------------------------


@dataclass(frozen=True)
class Capital:
    positions: int  # open positions (held or listed)
    tied_up: int  # buy prices of the open positions with a recorded purchase
    unknown_buy: int  # open positions without a recorded purchase (not in `tied_up`)
    market_value: int  # what selling everything at the market price would bring after tax
    without_market: int  # open positions without a current market price (not in the value)


def capital(session: Session, settings: Settings) -> Capital:
    """Capital in all open positions, whatever the portfolio view filters."""
    tied_up = unknown = value = missing = 0
    positions = repo.list_positions(session, OPEN_POSITIONS)
    for position in positions:
        if position.buy_price is None:
            unknown += 1
        else:
            tied_up += position.buy_price
        snapshot = repo.latest_snapshot(session, position.player, settings.platform)
        if snapshot is not None and snapshot.price is not None:
            value += net_after_tax(snapshot.price)
        else:
            missing += 1
    return Capital(len(positions), tied_up, unknown, value, missing)
