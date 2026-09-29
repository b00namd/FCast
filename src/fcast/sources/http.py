"""Polite HTTP client for web price sources.

Enforces the project rules for external sources: robots.txt is respected, at most one
request per host every `min_interval` seconds, responses are cached, and failures are
retried with exponential backoff. Only use it for sources whose terms allow automated access.
"""

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from fcast.sources.base import (
    PlayerNotFoundError,
    RobotsDisallowedError,
    SourceBlockedError,
    SourceError,
)

logger = logging.getLogger(__name__)

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]

RETRY_STATUSES = frozenset({500, 502, 503, 504})
# A site that answers like this wants us gone: no retries, the caller pauses the source.
BLOCK_STATUSES = frozenset({403, 429})
CHALLENGE_MARKERS = ("cf-chl", "challenge-platform", "Just a moment...", "Attention Required!")
MAX_BACKOFF_S = 120.0
ROBOTS_TTL_S = 24 * 3600.0
ROBOTS_AGENT = "FCast"
DENY_ALL = ["User-agent: *", "Disallow: /"]


class RateLimiter:
    """Ensures a minimum interval between requests to the same host."""

    def __init__(
        self, min_interval: float, clock: Clock = time.monotonic, sleep: Sleep = asyncio.sleep
    ) -> None:
        self.min_interval = min_interval
        self._clock = clock
        self._sleep = sleep
        self._last: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def wait(self, host: str) -> None:
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            last = self._last.get(host)
            if last is not None:
                delay = last + self.min_interval - self._clock()
                if delay > 0:
                    await self._sleep(delay)
            self._last[host] = self._clock()


class TTLCache[V]:
    def __init__(self, ttl: float, clock: Clock = time.monotonic) -> None:
        self.ttl = ttl
        self._clock = clock
        self._items: dict[str, tuple[float, V]] = {}

    def get(self, key: str) -> V | None:
        item = self._items.get(key)
        if item is None:
            return None
        expires, value = item
        if self._clock() >= expires:
            del self._items[key]
            return None
        return value

    def set(self, key: str, value: V) -> None:
        if self.ttl > 0:
            self._items[key] = (self._clock() + self.ttl, value)


def is_challenge(response: httpx.Response) -> bool:
    """Bot-protection interstitial (e.g. Cloudflare) instead of real content."""
    if response.status_code < 400:
        return False
    text = response.text[:20_000]
    return any(marker in text for marker in CHALLENGE_MARKERS)


def raise_if_blocked(response: httpx.Response, url: str) -> None:
    if is_challenge(response):
        raise SourceBlockedError(f"bot challenge (HTTP {response.status_code}) for {url}")
    if response.status_code in BLOCK_STATUSES:
        raise SourceBlockedError(f"HTTP {response.status_code} for {url}")


def _retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


class PoliteHttpClient:
    def __init__(
        self,
        user_agent: str,
        min_interval: float = 3.0,
        cache_ttl: float = 300.0,
        max_retries: int = 4,
        backoff_base: float = 2.0,
        timeout: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self._sleep = sleep
        self._clock = clock
        self._limiter = RateLimiter(min_interval, clock=clock, sleep=sleep)
        self._cache: TTLCache[str] = TTLCache(cache_ttl, clock=clock)
        self._robots: dict[str, tuple[float, RobotFileParser]] = {}
        self._client = httpx.AsyncClient(
            headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_text(self, url: str, cookies: dict[str, str] | None = None) -> str:
        """GET a page. `cookies` carries site preferences (e.g. platform), never sessions."""
        cookie_header = "; ".join(f"{key}={value}" for key, value in (cookies or {}).items())
        cache_key = f"{url}|{cookie_header}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        if not await self._allowed(url):
            raise RobotsDisallowedError(f"robots.txt disallows {url}")
        response = await self._request(url, {"Cookie": cookie_header} if cookie_header else None)
        raise_if_blocked(response, url)
        if response.status_code == 404:
            raise PlayerNotFoundError(f"not found: {url}")
        if response.status_code >= 400:
            raise SourceError(f"HTTP {response.status_code} for {url}")
        self._cache.set(cache_key, response.text)
        return response.text

    async def get_json(self, url: str, cookies: dict[str, str] | None = None) -> Any:
        try:
            return json.loads(await self.get_text(url, cookies))
        except json.JSONDecodeError as exc:
            raise SourceError(f"invalid JSON from {url}") from exc

    async def _request(self, url: str, headers: dict[str, str] | None = None) -> httpx.Response:
        """GET with rate limiting and retries. Returns the final response (any status)."""
        host = urlsplit(url).netloc
        attempt = 0
        while True:
            await self._limiter.wait(host)
            try:
                response = await self._client.get(url, headers=headers)
            except httpx.TransportError as exc:
                if attempt >= self.max_retries:
                    raise SourceError(f"request to {url} failed: {exc}") from exc
                delay = self._backoff(attempt)
                logger.warning("request to %s failed (%s), retrying in %.0fs", url, exc, delay)
            else:
                if (
                    response.status_code not in RETRY_STATUSES
                    or attempt >= self.max_retries
                    or is_challenge(response)
                ):
                    return response
                delay = max(self._backoff(attempt), _retry_after_seconds(response) or 0.0)
                delay = min(delay, MAX_BACKOFF_S)
                logger.warning(
                    "HTTP %s from %s, retrying in %.0fs", response.status_code, url, delay
                )
            await self._sleep(delay)
            attempt += 1

    def _backoff(self, attempt: int) -> float:
        return float(min(self.backoff_base * 2**attempt, MAX_BACKOFF_S))

    async def _allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        cached = self._robots.get(origin)
        if cached is None or self._clock() >= cached[0]:
            parser = await self._load_robots(origin)
            self._robots[origin] = (self._clock() + ROBOTS_TTL_S, parser)
        else:
            parser = cached[1]
        return parser.can_fetch(ROBOTS_AGENT, url)

    async def _load_robots(self, origin: str) -> RobotFileParser:
        """Fetch robots.txt following RFC 9309: 4xx allows everything, 5xx/errors deny all."""
        try:
            response = await self._request(f"{origin}/robots.txt")
        except SourceError:
            lines = DENY_ALL
        else:
            raise_if_blocked(response, f"{origin}/robots.txt")
            if response.status_code >= 500:
                lines = DENY_ALL
            elif response.status_code >= 400:
                lines = []
            else:
                lines = response.text.splitlines()
        parser = RobotFileParser()
        parser.parse(lines)
        parser.modified()  # can_fetch() denies everything until last_checked is set
        return parser
