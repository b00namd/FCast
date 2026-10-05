"""Portfolio overview shared by CLI and dashboard: profit after tax, market value, relists."""

from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from fcast.analysis.pricing import break_even_sell_price, profit
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
