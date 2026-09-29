"""PoliteHttpClient tests. All traffic goes through httpx.MockTransport, never the network."""

from collections.abc import Callable
from itertools import pairwise

import httpx
import pytest

from fcast.sources.base import (
    PlayerNotFoundError,
    RobotsDisallowedError,
    SourceBlockedError,
    SourceError,
)
from fcast.sources.http import PoliteHttpClient, RateLimiter, TTLCache

ROBOTS = "User-agent: *\nDisallow: /api/\n"


class FakeClock:
    """Monotonic clock whose sleep() advances time instantly and records the delays."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


Handler = Callable[[httpx.Request], httpx.Response]


class Recorder:
    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.requests: list[tuple[float, str]] = []
        self.clock: FakeClock | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert self.clock is not None
        self.requests.append((self.clock.now, str(request.url)))
        return self.handler(request)

    def paths(self) -> list[str]:
        return [url.split("example.com")[1] for _, url in self.requests]


def make_client(handler: Handler, **kwargs: float) -> tuple[PoliteHttpClient, Recorder, FakeClock]:
    clock = FakeClock()
    recorder = Recorder(handler)
    recorder.clock = clock
    client = PoliteHttpClient(
        user_agent="FCast/test",
        transport=httpx.MockTransport(recorder),
        clock=clock,
        sleep=clock.sleep,
        **kwargs,  # type: ignore[arg-type]
    )
    return client, recorder, clock


def site(pages: dict[str, httpx.Response]) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        return pages.get(request.url.path, httpx.Response(404))

    return handler


async def test_rate_limit_per_host() -> None:
    client, recorder, _ = make_client(
        site({"/a": httpx.Response(200, text="a"), "/b": httpx.Response(200, text="b")}),
        cache_ttl=0,
    )
    await client.get_text("https://example.com/a")
    await client.get_text("https://example.com/b")
    await client.get_text("https://example.com/a")
    await client.aclose()

    times = [t for t, _ in recorder.requests]
    assert recorder.paths() == ["/robots.txt", "/a", "/b", "/a"]
    gaps = [b - a for a, b in pairwise(times)]
    assert all(gap >= 3.0 for gap in gaps)


async def test_rate_limiter_hosts_are_independent() -> None:
    clock = FakeClock()
    limiter = RateLimiter(3.0, clock=clock, sleep=clock.sleep)
    await limiter.wait("a.com")
    await limiter.wait("b.com")
    assert clock.sleeps == []
    await limiter.wait("a.com")
    assert clock.sleeps == [3.0]


async def test_user_agent_is_sent() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["User-Agent"])
        return httpx.Response(200, text="" if request.url.path == "/robots.txt" else "ok")

    client, _, _ = make_client(handler)
    await client.get_text("https://example.com/x")
    await client.aclose()
    assert seen == ["FCast/test", "FCast/test"]


async def test_responses_are_cached() -> None:
    client, recorder, clock = make_client(site({"/p": httpx.Response(200, text="price")}))
    assert await client.get_text("https://example.com/p") == "price"
    assert await client.get_text("https://example.com/p") == "price"
    assert recorder.paths() == ["/robots.txt", "/p"]

    clock.now += 301  # cache TTL is 5 minutes
    await client.get_text("https://example.com/p")
    await client.aclose()
    assert recorder.paths() == ["/robots.txt", "/p", "/p"]


async def test_robots_disallow_blocks_request() -> None:
    client, recorder, _ = make_client(site({}))
    with pytest.raises(RobotsDisallowedError):
        await client.get_text("https://example.com/api/prices")
    await client.aclose()
    assert recorder.paths() == ["/robots.txt"]


@pytest.mark.parametrize(("status", "allowed"), [(404, True), (503, False)])
async def test_robots_unavailable(status: int, allowed: bool) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(status)
        return httpx.Response(200, text="ok")

    client, _, _ = make_client(handler, max_retries=0)
    if allowed:
        assert await client.get_text("https://example.com/x") == "ok"
    else:
        with pytest.raises(RobotsDisallowedError):
            await client.get_text("https://example.com/x")
    await client.aclose()


async def test_retry_with_exponential_backoff() -> None:
    responses = iter([httpx.Response(503), httpx.Response(502), httpx.Response(200, text="ok")])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return next(responses)

    client, _, clock = make_client(handler, backoff_base=2.0)
    assert await client.get_text("https://example.com/x") == "ok"
    await client.aclose()
    backoffs = [s for s in clock.sleeps if s not in (3.0,)]
    assert 2.0 in backoffs and 4.0 in backoffs


async def test_retry_after_header_is_honoured() -> None:
    responses = iter(
        [httpx.Response(503, headers={"Retry-After": "30"}), httpx.Response(200, text="ok")]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return next(responses)

    client, _, clock = make_client(handler)
    assert await client.get_text("https://example.com/x") == "ok"
    await client.aclose()
    assert 30.0 in clock.sleeps


async def test_gives_up_after_max_retries() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(500)

    client, recorder, _ = make_client(handler, max_retries=2)
    with pytest.raises(SourceError, match="HTTP 500"):
        await client.get_text("https://example.com/x")
    await client.aclose()
    assert recorder.paths().count("/x") == 3


async def test_transport_errors_are_retried_then_raised() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        raise httpx.ConnectError("boom", request=request)

    client, _, _ = make_client(handler, max_retries=1)
    with pytest.raises(SourceError, match="failed"):
        await client.get_text("https://example.com/x")
    await client.aclose()


async def test_404_is_player_not_found_and_not_retried() -> None:
    client, recorder, _ = make_client(site({}))
    with pytest.raises(PlayerNotFoundError):
        await client.get_text("https://example.com/player/1")
    await client.aclose()
    assert recorder.paths().count("/player/1") == 1


async def test_get_json() -> None:
    client, _, _ = make_client(
        site(
            {
                "/j": httpx.Response(200, text='{"price": 1000}'),
                "/bad": httpx.Response(200, text="<"),
            }
        )
    )
    assert await client.get_json("https://example.com/j") == {"price": 1000}
    with pytest.raises(SourceError, match="invalid JSON"):
        await client.get_json("https://example.com/bad")
    await client.aclose()


def test_ttl_cache_disabled_with_zero_ttl() -> None:
    cache: TTLCache[str] = TTLCache(0)
    cache.set("k", "v")
    assert cache.get("k") is None


@pytest.mark.parametrize("status", [403, 429])
async def test_block_statuses_are_not_retried(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(status)

    client, recorder, _ = make_client(handler)
    with pytest.raises(SourceBlockedError, match=f"HTTP {status}"):
        await client.get_text("https://example.com/x")
    await client.aclose()
    assert recorder.paths().count("/x") == 1


async def test_bot_challenge_is_detected_and_not_retried() -> None:
    challenge = "<html><title>Just a moment...</title><script src='/cdn-cgi/challenge-platform/x'>"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(503, text=challenge)

    client, recorder, _ = make_client(handler)
    with pytest.raises(SourceBlockedError, match="bot challenge"):
        await client.get_text("https://example.com/x")
    await client.aclose()
    assert recorder.paths().count("/x") == 1


async def test_blocked_robots_txt_counts_as_block() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="<title>Attention Required! | Cloudflare</title>")

    client, recorder, _ = make_client(handler)
    with pytest.raises(SourceBlockedError):
        await client.get_text("https://example.com/x")
    await client.aclose()
    assert recorder.paths() == ["/robots.txt"]


async def test_cookies_are_sent_and_part_of_cache_key() -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        seen.append(request.headers.get("Cookie"))
        return httpx.Response(200, text=request.headers.get("Cookie", "none"))

    client, _, _ = make_client(handler)
    assert await client.get_text("https://example.com/p", {"settings": "pc"}) == "settings=pc"
    assert await client.get_text("https://example.com/p", {"settings": "ps"}) == "settings=ps"
    assert await client.get_text("https://example.com/p", {"settings": "pc"}) == "settings=pc"
    assert await client.get_text("https://example.com/p") == "none"
    await client.aclose()
    assert seen == ["settings=pc", "settings=ps", None]
