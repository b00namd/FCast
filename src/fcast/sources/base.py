"""Price source interface and shared data types."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from fcast.config import Platform


class SourceError(Exception):
    """A source could not deliver data. The collector logs it and moves on."""


class PlayerNotFoundError(SourceError):
    pass


class NoPriceError(PlayerNotFoundError):
    """The card exists but no price is listed (e.g. untradeable or extinct)."""


class RobotsDisallowedError(SourceError):
    pass


class SourceBlockedError(SourceError):
    """The site is refusing us (403/429/bot challenge). The collector pauses the source."""


@dataclass(frozen=True)
class PriceQuote:
    ea_id: int
    platform: Platform
    price: int
    source: str
    captured_at: datetime


@dataclass(frozen=True)
class PlayerInfo:
    ea_id: int
    name: str | None = None
    rating: int | None = None
    position: str | None = None
    card_type: str | None = None
    league: str | None = None
    nation: str | None = None
    club: str | None = None


class PriceSource(ABC):
    """A read-only provider of public price data."""

    name: str
    # Remote sources are rotated per player to spread the load; local ones are always read.
    remote: bool = False

    @abstractmethod
    async def fetch_price(self, ea_id: int, platform: Platform) -> PriceQuote:
        """Current price for a card. Raises `PlayerNotFoundError` or `SourceError`."""

    @abstractmethod
    async def fetch_player(self, ea_id: int) -> PlayerInfo:
        """Card details. Raises `PlayerNotFoundError` or `SourceError`."""

    async def aclose(self) -> None:  # noqa: B027  (optional hook, default no-op)
        """Release resources such as HTTP connections."""
