"""FUTNext adapter tests against saved pages in tests/fixtures/futnext (no live requests)."""

import json
from pathlib import Path
from urllib.parse import unquote

import httpx
import pytest

from fcast.config import Platform
from fcast.sources.base import NoPriceError, UntradeableError
from fcast.sources.futnext import (
    FutnextFormatError,
    FutnextSource,
    parse_coins,
    parse_player,
    parse_price,
    platform_cookie,
    player_url,
)
from fcast.sources.http import PoliteHttpClient

FIXTURES = Path(__file__).parent / "fixtures" / "futnext"
CONSOLE = (FIXTURES / "231747-console.html").read_text(encoding="utf-8")
PC = (FIXTURES / "231747-pc.html").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("4.99M", 4_990_000),
        ("4.26M", 4_260_000),
        ("1M", 1_000_000),
        ("45.5K", 45_500),
        ("12k", 12_000),
        ("950", 950),
        ("1,25M", 1_250_000),
        ("PC", None),
        ("", None),
    ],
)
def test_parse_coins(text: str, value: int | None) -> None:
    assert parse_coins(text) == value


def test_parse_price_per_platform() -> None:
    assert parse_price(PC, Platform.PC) == 4_990_000
    assert parse_price(CONSOLE, Platform.CONSOLE) == 4_260_000


def test_wrong_platform_page_is_rejected() -> None:
    # Without the platform cookie FUTNext shows console prices; never store them as PC.
    with pytest.raises(FutnextFormatError, match="pc"):
        parse_price(CONSOLE, Platform.PC)


def test_zero_price_means_no_price() -> None:
    with pytest.raises(NoPriceError):
        parse_price(PC.replace(">4.99M<", ">0<"), Platform.PC)


@pytest.mark.parametrize("fixture", ["50583500-objective-pc.html", "84133907-sbc-pc.html"])
def test_sbc_and_objective_cards_are_untradeable(fixture: str) -> None:
    html = (FIXTURES / fixture).read_text(encoding="utf-8")
    with pytest.raises(UntradeableError):
        parse_price(html, Platform.PC)


def test_changed_layout_is_reported() -> None:
    with pytest.raises(FutnextFormatError):
        parse_price("<html><body>new</body></html>", Platform.PC)
    with pytest.raises(FutnextFormatError):
        parse_player("<html><title>Something else</title></html>", 1)


def test_parse_player() -> None:
    info = parse_player(PC, 231747)
    assert (info.name, info.rating, info.card_type) == ("Mbappé", 91, "Common")


def test_platform_cookie_format() -> None:
    cookie = platform_cookie(Platform.PC)
    assert json.loads(unquote(cookie["settings"])) == {"state": {"platform": "pc"}, "version": 0}
    assert json.loads(unquote(platform_cookie(Platform.CONSOLE)["settings"]))["state"] == {
        "platform": "ps"
    }


async def test_source_sends_platform_cookie_and_reuses_page() -> None:
    requests: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.url.path, request.headers.get("Cookie")))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /account\n")
        cookie = request.headers.get("Cookie", "")
        return httpx.Response(200, text=PC if "%22pc%22" in cookie else CONSOLE)

    async def no_sleep(_: float) -> None:
        return None

    client = PoliteHttpClient("FCast/test", transport=httpx.MockTransport(handler), sleep=no_sleep)
    source = FutnextSource(client, Platform.PC)
    quote = await source.fetch_price(231747, Platform.PC)
    info = await source.fetch_player(231747)
    await source.aclose()

    assert quote.price == 4_990_000
    assert quote.source == "futnext"
    assert info.rating == 91
    assert [path for path, _ in requests] == ["/robots.txt", "/players/p/231747"]
    assert requests[1][1] == "settings=" + platform_cookie(Platform.PC)["settings"]


def test_player_url() -> None:
    assert player_url(231747) == "https://www.futnext.com/players/p/231747"
