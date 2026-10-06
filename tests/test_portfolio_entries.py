"""Booking screenshot entries: card lookup, duplicates, unknown buys, listing history."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from fcast.config import Platform, Settings
from fcast.db import repositories as repo
from fcast.db.models import Player, PositionStatus
from fcast.portfolio import coins as wallet
from fcast.portfolio import lookup
from fcast.portfolio import summary as portfolio_summary
from fcast.portfolio.entries import Action, Entry, Outcome, book, parse_entries
from fcast.sources.futbin import FutbinSource
from tests.test_futbin_locator import make_client

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def card(
    session: Session, ea_id: int, name: str, rating: int, card_type: str = "Gold Rare"
) -> Player:
    return repo.upsert_player(
        session, ea_id, repo.PlayerDetails(name=name, rating=rating, card_type=card_type)
    )


@pytest.fixture
def cards(session: Session) -> dict[str, Player]:
    return {
        "musiala": card(session, 1, "Jamal Musiala", 87),
        "olise": card(session, 2, "Michael Olise", 85),
        "olise_totw": card(session, 3, "Michael Olise", 91, "Team of the Week"),
        "olise_holo": card(session, 4, "Michael Olise", 91, "Team of the Week (Holo)"),
        "wirtz": card(session, 5, "Florian Wirtz", 86),
    }


# --- lookup --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "name", "rating", "card_type", "ea_id"),
    [
        ("Musiala 87", "Musiala", 87, None, None),
        ("Olise 91 TOTW", "Olise", 91, "TOTW", None),
        (" Jamal  Musiala ", "Jamal Musiala", None, None, None),
        ("231747", "", None, None, 231747),
    ],
)
def test_parse_query(
    text: str, name: str, rating: int | None, card_type: str | None, ea_id: int | None
) -> None:
    query = lookup.parse_query(text)
    assert (query.name, query.rating, query.card_type, query.ea_id) == (
        name,
        rating,
        card_type,
        ea_id,
    )


def test_local_lookup(session: Session, cards: dict[str, Player]) -> None:
    def find(text: str) -> Player:
        query = lookup.parse_query(text)
        return lookup.pick(query, lookup.find_local(session, query))

    assert find("Musiala 87") is cards["musiala"]
    assert find("musiala") is cards["musiala"]  # case and first name do not matter
    assert find("Olise 85") is cards["olise"]
    assert find("Olise 91 TOTW") is cards["olise_totw"]  # alias, holo excluded
    assert find("Olise 91 TOTW Holo") is cards["olise_holo"]
    assert find("Olise 91") is cards["olise_totw"]  # the holo needs "Holo"
    assert find("Olise") is cards["olise"]  # several: the only base card wins
    with pytest.raises(lookup.CardLookupError, match="no card"):
        find("Musiala 88")
    with pytest.raises(lookup.CardLookupError, match="no card"):
        find("Ala 87")  # whole name parts only


def test_ambiguous_lookup_lists_the_cards(session: Session, cards: dict[str, Player]) -> None:
    card(session, 6, "Jamal Musiala", 87, "Team of the Week")
    card(session, 7, "Jamal Musiala", 87, "Team of the Season")
    query = lookup.parse_query("Musiala 87")
    found = lookup.find_local(session, query)
    del found[0]  # only the two special cards left: no base card to prefer
    with pytest.raises(lookup.CardLookupError, match=r"ambiguous.*\[6\].*\[7\]"):
        lookup.pick(query, found)


async def test_search_futbin_opens_candidates_until_one_matches() -> None:
    requests: list[str] = []
    source = FutbinSource(make_client(requests), lambda _: None)
    try:
        query = lookup.parse_query("Olise 91 TOTW")
        found = await lookup.search_futbin(source, query)
    finally:
        await source.aclose()
    assert [m.path for m in found] == ["/27/player/22947/michael-olise"]
    assert found[0].info.ea_id == 50_579_475
    pages = [r for r in requests if r.startswith("/27/player/") and "sitemap" not in r]
    # Special card: newest FUTBIN id first (22948 is the holo and is skipped).
    assert pages == ["/27/player/22948/michael-olise", "/27/player/22947/michael-olise"]


# --- booking -------------------------------------------------------------------------


async def test_buy_list_and_sell_by_name(session: Session, cards: dict[str, Player]) -> None:
    booking = await book(
        session,
        [
            Entry(Action.BUY, 45_000, card="Musiala 87"),
            Entry(Action.LISTED, 52_000, card="Musiala 87"),
        ],
        NOW,
    )
    assert [r.outcome for r in booking.results] == [Outcome.DONE, Outcome.DONE]
    assert booking.results[0].coins == -45_000
    position = repo.get_position(session, booking.results[0].position_id or 0)
    assert position.status is PositionStatus.LISTED

    later = NOW + timedelta(hours=2)
    booking = await book(session, [Entry(Action.SOLD, 52_000, card="Musiala 87")], later)
    assert booking.results[0].position_id == position.id
    assert booking.results[0].coins == 49_400  # 52.000 minus 5 % tax
    assert position.status is PositionStatus.SOLD


async def test_free_card_can_be_bought_at_zero_but_not_sold_at_zero(
    session: Session, cards: dict[str, Player]
) -> None:
    booking = await book(
        session,
        [
            Entry(Action.BUY, 0, card="Musiala 87"),
            Entry(Action.SOLD, 0, card="Musiala 87"),
        ],
        NOW,
    )
    assert [r.outcome for r in booking.results] == [Outcome.DONE, Outcome.ERROR]
    position = repo.get_position(session, booking.results[0].position_id or 0)
    assert position.buy_price == 0
    assert booking.results[0].coins == 0


async def test_screenshot_duplicates_are_skipped(
    session: Session, cards: dict[str, Player]
) -> None:
    first = [
        Entry(Action.BUY, 30_000, card="Wirtz 86"),
        Entry(Action.LISTED, 35_000, card="Wirtz 86"),
    ]
    await book(session, first, NOW)
    # The next screenshot still shows the same listing; the buy is entered twice by mistake.
    again = await book(session, first, NOW + timedelta(hours=1))
    assert [r.outcome for r in again.results] == [Outcome.SKIPPED, Outcome.SKIPPED]

    sold = [Entry(Action.SOLD, 35_000, card="Wirtz 86")]
    assert (await book(session, sold, NOW + timedelta(hours=2))).results[0].outcome is Outcome.DONE
    # Sold items stay in the transfer list until cleared.
    repeat = await book(session, sold, NOW + timedelta(hours=3))
    assert repeat.results[0].outcome is Outcome.SKIPPED
    assert "already sold" in repeat.results[0].note
    assert len(repo.list_positions(session)) == 1


async def test_relist_history_and_again(session: Session, cards: dict[str, Player]) -> None:
    await book(session, [Entry(Action.BUY, 30_000, card="Wirtz 86")], NOW)
    for hours, price in ((1, 36_000), (2, 34_000)):
        await book(
            session, [Entry(Action.LISTED, price, card="Wirtz 86")], NOW + timedelta(hours=hours)
        )
    # Expired and relisted at the same price: only booked with `again`.
    result = (
        await book(
            session,
            [Entry(Action.LISTED, 34_000, card="Wirtz 86", again=True)],
            NOW + timedelta(hours=3),
        )
    ).results[0]
    assert result.outcome is Outcome.DONE
    assert "(3x)" in result.note
    position = repo.list_positions(session)[0]
    assert [listing.price for listing in position.listings] == [36_000, 34_000, 34_000]


async def test_two_copies_are_booked_separately(session: Session, cards: dict[str, Player]) -> None:
    buys = [Entry(Action.BUY, 30_000, card="Wirtz 86"), Entry(Action.BUY, 31_000, card="Wirtz 86")]
    await book(session, buys, NOW)
    listings = [
        Entry(Action.LISTED, 35_000, card="Wirtz 86"),
        Entry(Action.LISTED, 35_000, card="Wirtz 86"),
    ]
    booking = await book(session, listings, NOW + timedelta(hours=1))
    assert [r.outcome for r in booking.results] == [Outcome.DONE, Outcome.DONE]
    assert booking.results[0].position_id != booking.results[1].position_id


async def test_sale_without_recorded_purchase(session: Session, cards: dict[str, Player]) -> None:
    booking = await book(session, [Entry(Action.SOLD, 20_000, card="Olise 85")], NOW)
    result = booking.results[0]
    assert result.outcome is Outcome.DONE
    assert "no recorded purchase" in result.note
    position = repo.get_position(session, result.position_id or 0)
    assert position.buy_price is None
    summary = portfolio_summary.summarize(session, Settings(_env_file=None), None)  # type: ignore[call-arg]
    assert summary.rows[0].profit is None
    assert summary.realised == 0


async def test_errors_are_reported_per_entry(session: Session, cards: dict[str, Player]) -> None:
    booking = await book(
        session,
        [
            Entry(Action.BUY, 10_000, card="Unbekannt 80"),
            Entry(Action.SOLD, 10_000, position_id=999),
            Entry(Action.BUY, 10_000, card="Musiala 87"),
        ],
        NOW,
    )
    assert [r.outcome for r in booking.results] == [Outcome.ERROR, Outcome.ERROR, Outcome.DONE]
    assert not booking.ok


async def test_screenshot_coins_are_not_counted_twice(
    session: Session, cards: dict[str, Player]
) -> None:
    wallet.set_balance(session, 100_000, NOW - timedelta(days=1))
    booking = await book(
        session,
        [Entry(Action.BUY, 45_000, card="Musiala 87")],
        NOW,
        coins=60_000,  # the screenshot shows the balance after the purchase
    )
    assert booking.balance_before == 100_000
    assert booking.balance_after == 60_000
    # Trades after the screenshot change the balance again.
    await book(session, [Entry(Action.SOLD, 50_000, card="Musiala 87")], NOW + timedelta(hours=1))
    balance = wallet.balance(session)
    assert balance is not None and balance.coins == 60_000 + 47_500


async def test_unknown_card_is_found_on_futbin_and_watched(session: Session) -> None:
    requests: list[str] = []
    source = FutbinSource(make_client(requests), lambda _: None)
    try:
        booking = await book(
            session, [Entry(Action.BUY, 120_000, card="Olise 91 TOTW")], NOW, source=source
        )
    finally:
        await source.aclose()
    result = booking.results[0]
    assert result.outcome is Outcome.DONE
    assert result.new_card
    player = repo.get_player_by_ea_id(session, 50_579_475)
    assert player is not None
    assert repo.get_watch(session, player) is not None
    assert repo.get_source_ref(session, 50_579_475, "futbin") == "/27/player/22947/michael-olise"


def test_relist_hint_when_market_is_lower(session: Session, cards: dict[str, Player]) -> None:
    settings = Settings(_env_file=None, platform="pc")  # type: ignore[call-arg]
    position = repo.open_position(session, cards["wirtz"], 30_000, NOW)
    repo.mark_listed(session, position, 36_000, NOW)
    repo.add_snapshot(session, cards["wirtz"], Platform.PC, 33_000, "futbin", NOW)
    row = portfolio_summary.position_row(session, settings, position)
    assert row.listings == 1 and not row.market_below_listing  # first listing: no hint yet

    repo.mark_listed(session, position, 36_000, NOW + timedelta(hours=1))
    row = portfolio_summary.position_row(session, settings, position)
    assert row.market_below_listing


# --- JSON ----------------------------------------------------------------------------


def test_parse_entries() -> None:
    entries, coins = parse_entries(
        {
            "coins": "250k",
            "entries": [
                {"action": "sold", "card": "Wirtz 86", "price": 30_000},
                {"action": "LISTED", "position": 12, "price": "45.000", "again": True},
            ],
        }
    )
    assert coins == 250_000
    assert entries == [
        Entry(Action.SOLD, 30_000, card="Wirtz 86"),
        Entry(Action.LISTED, 45_000, position_id=12, again=True),
    ]


@pytest.mark.parametrize(
    "data",
    [
        [],
        {"entries": [{"action": "gift", "card": "X", "price": 1}]},
        {"entries": [{"action": "buy", "price": 1}]},
        {"entries": [{"action": "buy", "card": "X", "position": 1, "price": 1}]},
        {"entries": [{"action": "buy", "card": "X", "price": "viel"}]},
    ],
)
def test_parse_entries_rejects_bad_input(data: object) -> None:
    with pytest.raises(ValueError):
        parse_entries(data)


# --- profit per period ---------------------------------------------------------------


def test_profit_per_day_week_and_total(session: Session, cards: dict[str, Player]) -> None:
    from zoneinfo import ZoneInfo

    berlin = ZoneInfo("Europe/Berlin")
    # Monday 05.10.2026, 12:00 UTC. Berlin is UTC+2 (summer time).
    now = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)

    def sale(buy: int | None, sell: int, sold_at: datetime) -> None:
        position = repo.open_position(session, cards["wirtz"], buy, sold_at - timedelta(hours=1))
        repo.sell_position(session, position, sell, sold_at)

    sale(10_000, 12_000, now - timedelta(hours=2))  # today: 11.400 - 10.000 = 1.400
    # Sunday 23:30 UTC = Monday 01:30 in Berlin: still today and this week.
    sale(5_000, 6_000, datetime(2026, 10, 4, 23, 30, tzinfo=UTC))  # 5.700 - 5.000 = 700
    # Sunday 21:00 UTC = 23:00 in Berlin: last week.
    sale(20_000, 20_000, datetime(2026, 10, 4, 21, 0, tzinfo=UTC))  # 19.000 - 20.000 = -1.000
    sale(None, 30_000, now - timedelta(hours=1))  # purchase not recorded: not in the profit
    sale(0, 1_000, now - timedelta(minutes=30))  # pack card: 950 profit

    report = portfolio_summary.profit_report(session, berlin, now)
    assert (report.today.profit, report.today.sales, report.today.unknown) == (3_050, 3, 1)
    assert report.week.profit == 3_050
    assert report.week.start.isoformat() == "2026-10-05"
    assert (report.total.profit, report.total.sales, report.total.unknown) == (2_050, 4, 1)
    assert [d.start.isoformat() for d in report.days] == ["2026-10-05", "2026-10-04"]
    assert [w.start.isoformat() for w in report.weeks] == ["2026-10-05", "2026-09-28"]
    assert report.weeks[1].profit == -1_000


def test_profit_report_without_sales(session: Session) -> None:
    from zoneinfo import ZoneInfo

    report = portfolio_summary.profit_report(session, ZoneInfo("Europe/Berlin"), NOW)
    assert (report.today.profit, report.week.profit, report.total.profit) == (0, 0, 0)
    assert report.days == [] and report.weeks == []


def test_capital_counts_all_open_positions(session: Session, cards: dict[str, Player]) -> None:
    settings = Settings(_env_file=None, platform="pc")  # type: ignore[call-arg]
    held = repo.open_position(session, cards["wirtz"], 30_000, NOW)
    listed = repo.open_position(session, cards["musiala"], 45_000, NOW)
    repo.mark_listed(session, listed, 52_000, NOW)
    repo.open_position(session, cards["olise"], None, NOW)  # from a pack
    sold = repo.open_position(session, cards["wirtz"], 28_000, NOW)
    repo.sell_position(session, sold, 35_000, NOW)  # not tied up any more
    repo.add_snapshot(session, cards["wirtz"], Platform.PC, 32_000, "futbin", NOW)
    repo.add_snapshot(session, cards["olise"], Platform.PC, 10_000, "futbin", NOW)
    del held

    capital = portfolio_summary.capital(session, settings)
    assert (capital.positions, capital.tied_up, capital.unknown_buy) == (3, 75_000, 1)
    # Wirtz 32.000 and Olise 10.000 after tax; Musiala has no market price.
    assert capital.market_value == 30_400 + 9_500
    assert capital.without_market == 1
