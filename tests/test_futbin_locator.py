"""Finding FUTBIN pages via the sitemap (fixture pages, mocked HTTP only)."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from fcast.collector.service import DISCOVERY_PREFIX, Collector
from fcast.config import Platform, Settings
from fcast.db import repositories as repo
from fcast.sources.futbin import FutbinSource
from fcast.sources.futbin_locator import (
    MAX_PAGES,
    FutbinLocator,
    name_hints_from_futgg,
    slugify,
)
from fcast.sources.http import PoliteHttpClient

FIXTURES = Path(__file__).parent / "fixtures" / "futbin"
OLISE = (FIXTURES / "22947-olise-totw.html").read_text(encoding="utf-8")
MARADONA = (FIXTURES / "21487-maradona.html").read_text(encoding="utf-8")

SITEMAP_INDEX = """<?xml version="1.0"?><sitemapindex>
<sitemap><loc>https://www.futbin.com/sitemap.xml</loc></sitemap>
<sitemap><loc>https://www.futbin.com/27/player/0/sitemap.xml</loc></sitemap>
<sitemap><loc>https://www.futbin.com/26/player/0/sitemap.xml</loc></sitemap>
</sitemapindex>"""
PLAYER_SITEMAP = """<?xml version="1.0"?><urlset>
<url><loc>https://www.futbin.com/27/player/22947/michael-olise</loc></url>
<url><loc>https://www.futbin.com/27/player/22947/michael-olise/evolutions</loc></url>
<url><loc>https://www.futbin.com/27/player/22948/michael-olise</loc></url>
<url><loc>https://www.futbin.com/27/player/62/michael-olise</loc></url>
<url><loc>https://www.futbin.com/27/player/21487/maradona</loc></url>
<url><loc>https://www.futbin.com/27/player/103/cole-palmer</loc></url>
<url><loc>https://www.futbin.com/27/player/8127/palmer-ault</loc></url>
</urlset>"""
PAGES = {
    "/sitemap_index.xml": SITEMAP_INDEX,
    "/27/player/0/sitemap.xml": PLAYER_SITEMAP,
    "/27/player/22947/michael-olise": OLISE,
    "/27/player/21487/maradona": MARADONA,
}


def make_client(requests: list[str]) -> PoliteHttpClient:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /*?*\n")
        page = PAGES.get(request.url.path)
        # Other versions exist but show a different card (Maradona's page as stand-in).
        if page is None and request.url.path.startswith("/27/player/"):
            page = MARADONA
        return httpx.Response(200, text=page) if page else httpx.Response(404)

    async def no_sleep(_: float) -> None:
        return None

    return PoliteHttpClient("FCast/test", transport=httpx.MockTransport(handler), sleep=no_sleep)


@pytest.mark.parametrize(
    ("name", "slug"),
    [("Kylian Mbappé", "kylian-mbappe"), ("Gerd Müller", "gerd-muller"), (" Son ", "son")],
)
def test_slugify(name: str, slug: str) -> None:
    assert slugify(name) == slug


def test_name_hints_from_futgg() -> None:
    link = "https://www.fut.gg/players/231747-kylian-mbappe/27-50563395/"
    assert name_hints_from_futgg(link) == ["kylian-mbappe"]
    assert name_hints_from_futgg("231747") == []


def test_candidate_order() -> None:
    index = {"michael-olise": [62, 22947, 22948], "cole-palmer": [103], "palmer-ault": [8127]}
    # Special card: newest FUTBIN ids first. Base card: oldest first.
    special = FutbinLocator.candidates(index, 50_579_475, ["Michael Olise"])
    assert special[0] == "/27/player/22948/michael-olise"
    base = FutbinLocator.candidates(index, 247_827, ["Michael Olise"])
    assert base[0] == "/27/player/62/michael-olise"
    # A short name finds every player whose slug ends with it.
    palmer = FutbinLocator.candidates(index, 257_534, ["Palmer"])
    assert palmer == ["/27/player/103/cole-palmer"]
    assert FutbinLocator.candidates(index, 1, ["Unbekannt"]) == []


async def test_find_special_card_via_sitemap() -> None:
    requests: list[str] = []
    locator = FutbinLocator(make_client(requests))
    path = await locator.find(50_579_475, ["Michael Olise"])
    assert path == "/27/player/22947/michael-olise"
    # Only the FC 27 player sitemap is read, never other years.
    assert "/26/player/0/sitemap.xml" not in requests
    # Newest first: 22948 is checked (wrong card) before 22947 matches.
    assert requests.index("/27/player/22948/michael-olise") < requests.index(
        "/27/player/22947/michael-olise"
    )


async def test_find_gives_up_after_max_pages() -> None:
    requests: list[str] = []
    locator = FutbinLocator(make_client(requests))
    assert await locator.find(999_999, ["Michael Olise", "Maradona", "Cole Palmer"]) is None
    pages = [r for r in requests if r.startswith("/27/player/") and "sitemap" not in r]
    assert len(pages) <= MAX_PAGES


async def test_index_is_cached() -> None:
    requests: list[str] = []
    now = [1000.0]
    locator = FutbinLocator(make_client(requests), clock=lambda: now[0])
    await locator.index()
    await locator.index()
    assert requests.count("/27/player/0/sitemap.xml") == 1


# --- collector integration -------------------------------------------------


def make_collector(tmp_path: Path, requests: list[str]) -> Collector:
    settings = Settings(_env_file=None, db_path=tmp_path / "db.sqlite", sources="", platform="pc")
    collector = Collector(settings, sources=[])
    collector.sources.append(
        FutbinSource(
            make_client(requests),
            lambda ea_id: collector._lookup_ref(ea_id, "futbin"),
            Platform.PC,
        )
    )
    return collector


def watch(collector: Collector, ea_id: int, name: str | None) -> None:
    with collector.session_factory.begin() as session:
        player = repo.upsert_player(session, ea_id, repo.PlayerDetails(name=name))
        repo.set_watch(session, player)


async def test_collector_discovers_missing_links(tmp_path: Path) -> None:
    requests: list[str] = []
    collector = make_collector(tmp_path, requests)
    watch(collector, 50_579_475, "Michael Olise")
    watch(collector, 1, None)  # no name yet: skipped until the first run fills it
    try:
        assert await collector.discover_futbin_links() == [50_579_475]
        with collector.session_factory() as session:
            assert repo.get_source_ref(session, 50_579_475, "futbin") == (
                "/27/player/22947/michael-olise"
            )
            player = repo.get_player_by_ea_id(session, 50_579_475)
            assert player is not None
            assert player.chem_style == "Hunter"
            assert f"{DISCOVERY_PREFIX}50579475" in repo.get_app_settings(session, DISCOVERY_PREFIX)
    finally:
        await collector.aclose()


async def test_unsuccessful_lookup_is_retried_only_after_a_day(tmp_path: Path) -> None:
    requests: list[str] = []
    collector = make_collector(tmp_path, requests)
    watch(collector, 999_999, "Nobody Known")
    try:
        assert await collector.discover_futbin_links() == []
        before = len(requests)
        assert await collector.discover_futbin_links() == []
        assert len(requests) == before  # not tried again today
        with collector.session_factory.begin() as session:
            yesterday = (datetime.now(UTC) - timedelta(days=2)).isoformat()
            repo.set_app_setting(session, f"{DISCOVERY_PREFIX}999999", yesterday)
        await collector.discover_futbin_links()
        with collector.session_factory() as session:
            attempt = repo.get_app_settings(session, DISCOVERY_PREFIX)[f"{DISCOVERY_PREFIX}999999"]
        assert attempt != yesterday  # tried again (sitemap cached, so no new requests)
    finally:
        await collector.aclose()


async def test_no_lookup_while_futbin_is_paused(tmp_path: Path) -> None:
    requests: list[str] = []
    collector = make_collector(tmp_path, requests)
    watch(collector, 50_579_475, "Michael Olise")
    with collector.session_factory.begin() as session:
        repo.pause_source(session, "futbin", datetime.now(UTC) + timedelta(hours=1), "HTTP 429")
    try:
        assert await collector.discover_futbin_links() == []
        assert requests == []
    finally:
        await collector.aclose()
