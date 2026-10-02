"""FUTBIN adapter tests against saved pages in tests/fixtures/futbin (no live requests)."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from fcast.config import Platform
from fcast.sources.base import ExtinctError, NoPriceError, PlayerNotFoundError, UntradeableError
from fcast.sources.futbin import (
    FutbinFormatError,
    FutbinSource,
    normalize_ref,
    parse_player,
    parse_price,
)
from fcast.sources.http import PoliteHttpClient

FIXTURES = Path(__file__).parent / "fixtures" / "futbin"
MARADONA = (FIXTURES / "21487-maradona.html").read_text(encoding="utf-8")
MULLER = (FIXTURES / "21516-muller.html").read_text(encoding="utf-8")
NOW = datetime(2026, 9, 29, 19, 0, tzinfo=UTC)


PAGES = {"maradona": MARADONA, "muller": MULLER}


@pytest.mark.parametrize(
    ("page", "platform", "price"),
    [
        ("maradona", Platform.CONSOLE, 8_150_000),
        ("maradona", Platform.PC, 6_800_000),
        ("muller", Platform.CONSOLE, 1_040_000),
        ("muller", Platform.PC, 1_300_000),
    ],
)
def test_parse_price(page: str, platform: Platform, price: int) -> None:
    assert parse_price(PAGES[page], platform, now=NOW).price == price


def test_parse_price_update_time() -> None:
    console_time = parse_price(MARADONA, Platform.CONSOLE, now=NOW).updated
    pc_time = parse_price(MARADONA, Platform.PC, now=NOW).updated
    assert console_time == NOW - timedelta(minutes=1)
    assert pc_time == NOW - timedelta(minutes=2)


@pytest.mark.parametrize(
    ("text", "delta"),
    [
        ("Price Updated: 30 secs ago", timedelta(seconds=30)),
        ("Price Updated: 1 hour ago", timedelta(hours=1)),
        ("Price Updated: 3 days ago", timedelta(days=3)),
        ("Price Updated: Never", timedelta(0)),
    ],
)
def test_update_time_formats(text: str, delta: timedelta) -> None:
    html = MARADONA.replace("Price Updated: 1 mins ago", text, 1)
    updated = parse_price(html, Platform.CONSOLE, now=NOW).updated
    assert updated == NOW - delta


def test_extinct_card_keeps_price_range() -> None:
    html = MARADONA.replace("8,150,000", "0", 1)
    with pytest.raises(ExtinctError) as info:
        parse_price(html, Platform.CONSOLE, now=NOW)
    assert isinstance(info.value, NoPriceError)  # older callers treat it as "no price"
    assert info.value.market.extinct
    assert (info.value.market.range_min, info.value.market.range_max) == (75_000, 14_500_000)


def test_listings_and_price_range() -> None:
    market = parse_price(MARADONA, Platform.CONSOLE, now=NOW).market
    assert market.listings == (8_150_000,)
    assert (market.range_min, market.range_max) == (75_000, 14_500_000)


def test_multiple_listings_are_sorted() -> None:
    # Fill the first of the four "further lowest prices" (0 in the fixture) with a value.
    marker = 'class="lowest-price inline-with-icon">0<'
    assert marker in MARADONA
    html = MARADONA.replace(marker, 'class="lowest-price inline-with-icon">9,000,000<', 1)
    parsed = parse_price(html, Platform.CONSOLE, now=NOW)
    assert parsed.market.listings == (8_150_000, 9_000_000)
    assert parsed.price == 8_150_000


@pytest.mark.parametrize("fixture", ["21758-adeyemi-objective.html", "23101-olise-sbc.html"])
@pytest.mark.parametrize("platform", list(Platform))
def test_sbc_and_objective_cards_are_untradeable(fixture: str, platform: Platform) -> None:
    html = (FIXTURES / fixture).read_text(encoding="utf-8")
    with pytest.raises(UntradeableError):
        parse_price(html, platform, now=NOW)


def test_changed_layout_is_reported() -> None:
    with pytest.raises(FutbinFormatError):
        parse_price("<html><body>new layout</body></html>", Platform.CONSOLE)
    with pytest.raises(FutbinFormatError):
        parse_player("<html></html>", 1)


def test_parse_player() -> None:
    info = parse_player(MARADONA, 190042)
    assert info.ea_id == 190042
    assert info.name == "Diego Maradona"
    assert info.rating == 95
    assert info.position == "CAM"
    assert info.card_type == "Base Icon"
    assert (info.league, info.nation, info.club) == ("Icons", "Argentina", "EA FC ICONS")

    muller = parse_player(MULLER, 190048)
    assert muller.name == "Gerd Müller"
    assert (muller.rating, muller.position, muller.nation) == (92, "ST", "Germany")


@pytest.mark.parametrize(
    "value",
    [
        "https://www.futbin.com/27/player/21487/maradona",
        "https://futbin.com/27/player/21487/maradona/",
        "http://www.futbin.com/27/player/21487/maradona?page=1#prices",
        "/27/player/21487/maradona",
    ],
)
def test_normalize_ref(value: str) -> None:
    assert normalize_ref(value) == "/27/player/21487/maradona"


@pytest.mark.parametrize(
    "value", ["https://www.fut.gg/players/231747/", "/27/player/abc/x", "21487", ""]
)
def test_normalize_ref_rejects_other_urls(value: str) -> None:
    with pytest.raises(ValueError, match="FUTBIN"):
        normalize_ref(value)


def make_source(refs: dict[int, str]) -> tuple[FutbinSource, list[str]]:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /*?*\n")
        if request.url.path == "/27/player/21487/maradona":
            return httpx.Response(200, text=MARADONA)
        return httpx.Response(404)

    async def no_sleep(_: float) -> None:
        return None

    client = PoliteHttpClient("FCast/test", transport=httpx.MockTransport(handler), sleep=no_sleep)
    return FutbinSource(client, refs.get), requested


async def test_source_fetches_price_and_player_with_one_page_request() -> None:
    source, requested = make_source({190042: "/27/player/21487/maradona"})
    quote = await source.fetch_price(190042, Platform.CONSOLE)
    info = await source.fetch_player(190042)
    await source.aclose()

    assert quote.price == 8_150_000
    assert quote.source == "futbin"
    assert quote.ea_id == 190042
    assert info.name == "Diego Maradona"
    assert requested == ["/robots.txt", "/27/player/21487/maradona"]


async def test_source_without_stored_link() -> None:
    source, requested = make_source({})
    with pytest.raises(PlayerNotFoundError, match="no FUTBIN link"):
        await source.fetch_price(1, Platform.CONSOLE)
    await source.aclose()
    assert requested == []


async def test_source_unknown_page() -> None:
    source, _ = make_source({1: "/27/player/1/nobody"})
    with pytest.raises(PlayerNotFoundError):
        await source.fetch_price(1, Platform.PC)
    await source.aclose()


def test_source_is_remote() -> None:
    assert FutbinSource.remote is True


@pytest.mark.parametrize(
    ("page", "styles"),
    [
        ("maradona", (("Hunter", 60), ("Hawk", 40), ("Basic", 0))),
        ("muller", (("Engine", 50), ("Guardian", 50), ("Basic", 0))),
    ],
)
def test_community_chem_styles(page: str, styles: tuple[tuple[str, int], ...]) -> None:
    from fcast.sources.futbin import format_chem_styles, parse_community_chem_styles

    parsed = parse_community_chem_styles(PAGES[page])
    assert parsed == styles
    info = parse_player(PAGES[page], 1, Platform.PC)
    assert info.chem_style == styles[0][0]
    assert info.chem_styles == format_chem_styles(styles)


def test_chem_styles_fallback_to_bio_sentence() -> None:
    from fcast.sources.futbin import parse_usage

    html = MARADONA.replace("community-chem-styles", "something-else")
    assert parse_usage(html, Platform.PC).chem_style == "Basic"  # PC sentence in the bio
    assert parse_usage(html, Platform.PC).chem_styles == ()
