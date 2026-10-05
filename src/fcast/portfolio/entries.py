"""Book purchases, listings and sales as read from a transfer-list screenshot.

`book` writes every entry into the session; the caller commits (apply) or rolls back
(preview). Screenshots show the same items again until the user clears them, so entries that
look already booked are skipped unless `again` is set:

- buy: same card and price bought within the last day
- listed: the card is already listed at that price
- sold: a copy of the card was sold at that price within the last days and not booked in this
  run yet

A sale or listing without an open position creates one with unknown buy price (pack, reward,
bought before FCast): it counts for the coin balance but not for the profit.
"""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from fcast.analysis.pricing import net_after_tax
from fcast.db import repositories as repo
from fcast.db.models import Player, PortfolioPosition, PositionStatus
from fcast.portfolio import coins as wallet
from fcast.portfolio import lookup
from fcast.sources import futbin

DUPLICATE_BUY = timedelta(days=1)
DUPLICATE_SALE = timedelta(days=3)
PORTFOLIO_NOTE = "Portfolio"


class Action(StrEnum):
    BUY = "buy"
    LISTED = "listed"
    SOLD = "sold"


class Outcome(StrEnum):
    DONE = "done"
    SKIPPED = "skipped"
    ERROR = "error"


@dataclass(frozen=True)
class Entry:
    action: Action
    price: int
    card: str | None = None  # "Musiala 87", "Olise 91 TOTW" or an EA id
    position_id: int | None = None  # instead of `card`
    again: bool = False  # book even if it looks like a duplicate


@dataclass(frozen=True)
class Result:
    entry: Entry
    outcome: Outcome
    note: str
    card: str | None = None  # display name with rating and type
    position_id: int | None = None
    coins: int = 0  # effect on the coin balance
    new_card: bool = False  # found on FUTBIN, added to the database and watchlist


@dataclass
class Booking:
    results: list[Result] = field(default_factory=list)
    balance_before: int | None = None  # FCast's balance before the entries
    balance_after: int | None = None  # after the entries (and the screenshot's value)

    @property
    def ok(self) -> bool:
        return all(result.outcome is not Outcome.ERROR for result in self.results)

    def count(self, outcome: Outcome) -> int:
        return sum(1 for result in self.results if result.outcome is outcome)


def describe(player: Player) -> str:
    """'Jamal Musiala (87) Gold Rare [1]'."""
    card_type = f" {player.card_type}" if player.card_type else ""
    return f"{player.display_name}{card_type} [{player.ea_id}]"


class _Run:
    def __init__(self, session: Session, now: datetime, source: futbin.FutbinSource | None) -> None:
        self.session = session
        self.now = now
        self.source = source
        # Positions already used per action in this run: a copy bought in this run can still
        # be listed and sold, but two listing entries never hit the same copy.
        self.claimed: dict[Action, set[int]] = defaultdict(set)

    async def card(self, text: str) -> tuple[Player, bool]:
        """The card meant by `text`; looked up on FUTBIN (and stored) if unknown."""
        query = lookup.parse_query(text)
        found = lookup.find_local(self.session, query)
        if found or self.source is None or query.ea_id is not None:
            return lookup.pick(query, found), False
        matches = await lookup.search_futbin(self.source, query)
        if not matches:
            raise lookup.CardLookupError(f"no card matches {text!r}, not on FUTBIN either")
        from fcast.collector.job import apply_player_info

        match = matches[0]
        player = apply_player_info(self.session, match.info)
        repo.set_source_ref(self.session, player, futbin.SOURCE_NAME, match.path)
        if repo.get_watch(self.session, player) is None:
            repo.set_watch(self.session, player, note=PORTFOLIO_NOTE)  # keep its price current
        return player, True

    def _open(self, player: Player, action: Action) -> list[PortfolioPosition]:
        claimed = self.claimed[action]
        return [p for p in repo.open_positions_of(self.session, player) if p.id not in claimed]

    def _claim(self, position: PortfolioPosition, action: Action) -> PortfolioPosition:
        self.claimed[action].add(position.id)
        return position

    def _unknown_buy(self, player: Player, action: Action) -> PortfolioPosition:
        position = repo.open_position(self.session, player, None, bought_at=self.now)
        return self._claim(position, action)

    async def book(self, entry: Entry) -> Result:
        new_card = False
        if entry.position_id is not None:
            position = repo.get_position(self.session, entry.position_id)
            player = position.player
            if position.id in self.claimed[entry.action]:
                raise repo.InvalidStateError(f"position {position.id} is used twice")
        elif entry.card is not None:
            player, new_card = await self.card(entry.card)
            position = None
        else:
            raise ValueError("entry needs a card or a position")
        result = {
            Action.BUY: self._buy,
            Action.LISTED: self._listed,
            Action.SOLD: self._sold,
        }[entry.action](entry, player, position)
        if new_card:
            result = replace(result, new_card=True)
        return result

    def _buy(self, entry: Entry, player: Player, position: PortfolioPosition | None) -> Result:
        if position is not None:
            raise ValueError("a purchase needs a card, not a position")
        if not entry.again:
            since = self.now - DUPLICATE_BUY
            for existing in self.session.scalars(
                select(PortfolioPosition).where(
                    PortfolioPosition.player_id == player.id,
                    PortfolioPosition.buy_price == entry.price,
                    PortfolioPosition.bought_at >= since,
                )
            ):
                if existing.id not in self.claimed[entry.action]:
                    self._claim(existing, entry.action)
                    return Result(
                        entry,
                        Outcome.SKIPPED,
                        f"already bought at this price (#{existing.id})",
                        describe(player),
                        existing.id,
                    )
        bought = repo.open_position(self.session, player, entry.price, self.now)
        self._claim(bought, Action.BUY)
        return Result(entry, Outcome.DONE, "bought", describe(player), bought.id, -entry.price)

    def _listed(self, entry: Entry, player: Player, position: PortfolioPosition | None) -> Result:
        if position is None:
            candidates = self._open(player, entry.action)
            same = [
                p
                for p in candidates
                if p.status is PositionStatus.LISTED and p.sell_price == entry.price
            ]
            position = next(iter(same or candidates), None)
        note = "listed" if position is None or not position.listings else "relisted"
        if position is None:
            position = self._unknown_buy(player, entry.action)
            note = "listed, no recorded purchase"
        self._claim(position, entry.action)
        if (
            not entry.again
            and position.status is PositionStatus.LISTED
            and position.sell_price == entry.price
        ):
            return Result(
                entry,
                Outcome.SKIPPED,
                "already listed at this price",
                describe(player),
                position.id,
            )
        repo.mark_listed(self.session, position, entry.price, self.now)
        count = len(position.listings)
        if count > 1:
            note = f"{note} ({count}x)"
        return Result(entry, Outcome.DONE, note, describe(player), position.id)

    def _sold(self, entry: Entry, player: Player, position: PortfolioPosition | None) -> Result:
        if position is None and not entry.again:
            since = self.now - DUPLICATE_SALE
            for existing in self.session.scalars(
                select(PortfolioPosition).where(
                    PortfolioPosition.player_id == player.id,
                    PortfolioPosition.status == PositionStatus.SOLD,
                    PortfolioPosition.sell_price == entry.price,
                    PortfolioPosition.sold_at >= since,
                )
            ):
                if existing.id not in self.claimed[entry.action]:
                    self._claim(existing, entry.action)
                    return Result(
                        entry,
                        Outcome.SKIPPED,
                        f"already sold at this price (#{existing.id})",
                        describe(player),
                        existing.id,
                    )
        note = "sold"
        if position is None:
            candidates = self._open(player, entry.action)
            listed = [p for p in candidates if p.status is PositionStatus.LISTED]
            same = [p for p in listed if p.sell_price == entry.price]
            position = next(iter(same or listed or candidates), None)
        if position is None:
            position = self._unknown_buy(player, entry.action)
            note = "sold, no recorded purchase"
        self._claim(position, entry.action)
        repo.sell_position(self.session, position, entry.price, self.now)
        return Result(
            entry,
            Outcome.DONE,
            note,
            describe(player),
            position.id,
            net_after_tax(entry.price),
        )


async def book(
    session: Session,
    entries: Sequence[Entry],
    now: datetime,
    coins: int | None = None,
    source: futbin.FutbinSource | None = None,
) -> Booking:
    """Write all entries (and the screenshot's coin balance) into `session`.

    Errors do not stop the run, so a preview lists every problem; commit only if `ok`.
    """
    booking = Booking()
    before = wallet.balance(session)
    booking.balance_before = before.coins if before is not None else None
    run = _Run(session, now, source)
    for entry in entries:
        try:
            if entry.price <= 0:
                raise ValueError("price must be positive")
            booking.results.append(await run.book(entry))
        except (lookup.CardLookupError, repo.NotFoundError, repo.InvalidStateError) as exc:
            booking.results.append(Result(entry, Outcome.ERROR, str(exc)))
        except ValueError as exc:
            booking.results.append(Result(entry, Outcome.ERROR, str(exc)))
    if coins is not None:
        # The screenshot already includes these trades: entered at the same moment, they
        # are not counted again (only trades after `now` change the balance).
        wallet.set_balance(session, coins, now)
    after = wallet.balance(session)
    booking.balance_after = after.coins if after is not None else None
    return booking


def _price(value: object) -> int:
    from fcast.web.forms import parse_coins

    if isinstance(value, bool):
        raise ValueError(f"invalid price: {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        price = parse_coins(value)
        if price is not None:
            return price
    raise ValueError(f"invalid price: {value!r}")


def parse_entries(data: object) -> tuple[list[Entry], int | None]:
    """Entries and coin balance from the JSON of `fcast portfolio apply`."""
    if not isinstance(data, dict):
        raise ValueError('expected an object like {"coins": 250000, "entries": [...]}')
    coins = data.get("coins")
    if coins is not None:
        coins = _price(coins)
    raw = data.get("entries", [])
    if not isinstance(raw, list):
        raise ValueError('"entries" must be a list')
    entries = []
    for i, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"entry {i}: expected an object")
        try:
            action = Action(str(item.get("action", "")).lower())
        except ValueError:
            raise ValueError(
                f"entry {i}: action must be one of {', '.join(a.value for a in Action)}"
            ) from None
        card = item.get("card")
        position = item.get("position")
        if (card is None) == (position is None):
            raise ValueError(f"entry {i}: give either card or position")
        entries.append(
            Entry(
                action,
                _price(item.get("price")),
                card=str(card) if card is not None else None,
                position_id=int(position) if position is not None else None,
                again=bool(item.get("again", False)),
            )
        )
    return entries, coins
