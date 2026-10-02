"""Price source interface and shared data types."""

import json
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime

from fcast.config import Platform


@dataclass(frozen=True)
class CardAttributes:
    """In-game attributes of a card as FUTBIN shows them (English stat names)."""

    stats: dict[str, int] = field(default_factory=dict)  # face stats and detailed stats
    playstyles: tuple[str, ...] = ()  # all PlayStyles, including the PlayStyle+ ones
    playstyles_plus: tuple[str, ...] = ()
    skills: int | None = None
    weak_foot: int | None = None
    height_cm: int | None = None
    body_type: str | None = None
    foot: str | None = None
    accelerate: str | None = None  # Explosive, Controlled or Lengthy (without chem style)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str | None) -> "CardAttributes | None":
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return cls(
                stats={str(k): int(v) for k, v in data.get("stats", {}).items()},
                playstyles=tuple(data.get("playstyles", ())),
                playstyles_plus=tuple(data.get("playstyles_plus", ())),
                skills=data.get("skills"),
                weak_foot=data.get("weak_foot"),
                height_cm=data.get("height_cm"),
                body_type=data.get("body_type"),
                foot=data.get("foot"),
                accelerate=data.get("accelerate"),
            )
        except (ValueError, TypeError, AttributeError):
            return None


class SourceError(Exception):
    """A source could not deliver data. The collector logs it and moves on."""


class PlayerNotFoundError(SourceError):
    pass


class NoPriceError(PlayerNotFoundError):
    """The card exists but no price is listed (e.g. untradeable or extinct)."""


class UntradeableError(NoPriceError):
    """SBC or objective reward: the card never appears on the transfer market."""


class ExtinctError(NoPriceError):
    """No listings at all. Carries the market info so the collector can record it."""

    def __init__(self, message: str, market: "MarketInfo") -> None:
        super().__init__(message)
        self.market = market


class RobotsDisallowedError(SourceError):
    pass


class SourceBlockedError(SourceError):
    """The site is refusing us (403/429/bot challenge). The collector pauses the source."""


@dataclass(frozen=True)
class MarketInfo:
    """Supply details as far as a source shows them."""

    listings: tuple[int, ...] = ()  # lowest BINs, ascending
    range_min: int | None = None  # EA price range
    range_max: int | None = None

    @property
    def extinct(self) -> bool:
        return not self.listings


@dataclass(frozen=True)
class PriceQuote:
    ea_id: int
    platform: Platform
    price: int
    source: str
    captured_at: datetime
    market: MarketInfo | None = None


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
    # Usage data for the configured platform (FUTBIN only)
    chem_style: str | None = None
    chem_styles: str | None = None  # top 3 as "Hunter:77|Artist:8|Engine:8"
    games_used: int | None = None
    goals_per_game: float | None = None
    attributes: CardAttributes | None = None  # stats, PlayStyles, AcceleRATE (FUTBIN only)


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
