"""ORM models. Prices are whole coins, all timestamps are UTC."""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from fcast.config import Platform
from fcast.db.base import Base, UTCDateTime, str_enum, utcnow
from fcast.sources.base import CardAttributes


def parse_chem_styles(raw: str | None) -> list[tuple[str, int]]:
    """ "Hunter:77|Artist:8" -> [("Hunter", 77), ("Artist", 8)]."""
    styles = []
    for item in (raw or "").split("|"):
        name, _, share = item.rpartition(":")
        if name and share.isdigit():
            styles.append((name, int(share)))
    return styles


class PositionStatus(StrEnum):
    HOLDING = "holding"
    LISTED = "listed"
    SOLD = "sold"


class LinkType(StrEnum):
    PLAYER = "player"
    LEAGUE = "league"
    NATION = "nation"
    CLUB = "club"


class Player(Base):
    """A single card. Only `ea_id` is required; details are filled in by price sources."""

    __tablename__ = "players"

    id: Mapped[int] = mapped_column(primary_key=True)
    ea_id: Mapped[int] = mapped_column(unique=True)
    name: Mapped[str | None] = mapped_column(String(100))
    rating: Mapped[int | None]
    position: Mapped[str | None] = mapped_column(String(8))
    card_type: Mapped[str | None] = mapped_column(String(50))
    league: Mapped[str | None] = mapped_column(String(100))
    nation: Mapped[str | None] = mapped_column(String(100))
    club: Mapped[str | None] = mapped_column(String(100))
    # FUTBIN usage data for the configured platform: recommended chem style and games played
    chem_style: Mapped[str | None] = mapped_column(String(32))
    # Top 3 community chem styles with share: "Hunter:77|Artist:8|Engine:8"
    chem_styles_raw: Mapped[str | None] = mapped_column("chem_styles", String(120))
    games_used: Mapped[int | None]
    goals_per_game: Mapped[float | None]
    # In-game attributes (stats, PlayStyles, AcceleRATE) as JSON, see CardAttributes.
    attributes_raw: Mapped[str | None] = mapped_column("attributes", Text)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    watchlist_entry: Mapped["WatchlistEntry | None"] = relationship(
        back_populates="player", cascade="all, delete-orphan", passive_deletes=True
    )

    @property
    def chem_styles(self) -> list[tuple[str, int]]:
        return parse_chem_styles(self.chem_styles_raw)

    @property
    def attributes(self) -> CardAttributes | None:
        return CardAttributes.from_json(self.attributes_raw)

    @property
    def display_name(self) -> str:
        if self.name is None:
            return f"#{self.ea_id}"
        return f"{self.name} ({self.rating})" if self.rating is not None else self.name


class PriceSnapshot(Base):
    __tablename__ = "price_snapshots"
    __table_args__ = (
        Index("ix_price_snapshots_player_id_captured_at", "player_id", "captured_at"),
        CheckConstraint("price > 0", name="price_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id", ondelete="CASCADE"))
    platform: Mapped[Platform] = mapped_column(str_enum(Platform))
    price: Mapped[int]
    source: Mapped[str] = mapped_column(String(32))
    captured_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class WatchlistEntry(Base):
    __tablename__ = "watchlist"
    __table_args__ = (
        CheckConstraint("target_buy IS NULL OR target_buy > 0", name="target_buy_positive"),
        CheckConstraint("target_sell IS NULL OR target_sell > 0", name="target_sell_positive"),
    )

    player_id: Mapped[int] = mapped_column(
        ForeignKey("players.id", ondelete="CASCADE"), primary_key=True
    )
    target_buy: Mapped[int | None]
    target_sell: Mapped[int | None]
    note: Mapped[str | None] = mapped_column(String(500))
    active: Mapped[bool] = mapped_column(default=True)
    # Collect every `interval_min` minutes (None: every collector run) and when it was last done.
    interval_min: Mapped[int | None]
    checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    player: Mapped[Player] = relationship(back_populates="watchlist_entry")


class PortfolioPosition(Base):
    __tablename__ = "portfolio"
    __table_args__ = (
        CheckConstraint("buy_price IS NULL OR buy_price >= 0", name="buy_price_not_negative"),
        CheckConstraint("sell_price IS NULL OR sell_price > 0", name="sell_price_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id", ondelete="RESTRICT"))
    # 0: free card (pack, reward); None: the purchase was never recorded (e.g. before FCast).
    buy_price: Mapped[int | None]
    bought_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    # Listing price while `listed`, final price once `sold`.
    sell_price: Mapped[int | None]
    sold_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    status: Mapped[PositionStatus] = mapped_column(
        str_enum(PositionStatus), default=PositionStatus.HOLDING
    )

    player: Mapped[Player] = relationship()
    listings: Mapped[list["PortfolioListing"]] = relationship(
        back_populates="position",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="PortfolioListing.listed_at",
    )


class PortfolioListing(Base):
    """Every time a position was put on the transfer market, including relists."""

    __tablename__ = "portfolio_listings"
    __table_args__ = (CheckConstraint("price > 0", name="price_positive"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    position_id: Mapped[int] = mapped_column(
        ForeignKey("portfolio.id", ondelete="CASCADE"), index=True
    )
    price: Mapped[int]
    listed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    position: Mapped[PortfolioPosition] = relationship(back_populates="listings")


class Promo(Base):
    __tablename__ = "promos"
    __table_args__ = (
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    starts_at: Mapped[datetime] = mapped_column(UTCDateTime)
    ends_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    source: Mapped[str | None] = mapped_column(String(200))
    confidence: Mapped[float] = mapped_column(default=0.5)
    note: Mapped[str | None] = mapped_column(String(500))

    links: Mapped[list["PromoLink"]] = relationship(
        back_populates="promo", cascade="all, delete-orphan", passive_deletes=True
    )


class PromoLink(Base):
    __tablename__ = "promo_links"

    promo_id: Mapped[int] = mapped_column(
        ForeignKey("promos.id", ondelete="CASCADE"), primary_key=True
    )
    link_type: Mapped[LinkType] = mapped_column(str_enum(LinkType), primary_key=True)
    link_value: Mapped[str] = mapped_column(String(100), primary_key=True)

    promo: Mapped[Promo] = relationship(back_populates="links")


class AlertLog(Base):
    __tablename__ = "alerts_log"
    __table_args__ = (
        Index("ix_alerts_log_rule_player_id_sent_at", "rule", "player_id", "sent_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    rule: Mapped[str] = mapped_column(String(32))
    player_id: Mapped[int | None] = mapped_column(ForeignKey("players.id", ondelete="SET NULL"))
    message: Mapped[str]
    sent_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class SourceRef(Base):
    """How a web source identifies a card, e.g. the FUTBIN page path for an EA card id."""

    __tablename__ = "source_refs"

    player_id: Mapped[int] = mapped_column(
        ForeignKey("players.id", ondelete="CASCADE"), primary_key=True
    )
    source: Mapped[str] = mapped_column(String(32), primary_key=True)
    external_ref: Mapped[str] = mapped_column(String(200))


class SourceStatus(Base):
    """Health of a price source; `paused_until` is set when a site starts refusing us."""

    __tablename__ = "source_status"

    source: Mapped[str] = mapped_column(String(32), primary_key=True)
    paused_until: Mapped[datetime | None] = mapped_column(UTCDateTime)
    pause_reason: Mapped[str | None] = mapped_column(String(500))
    last_success_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_error_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_error: Mapped[str | None] = mapped_column(String(500))


class MarketState(Base):
    """Latest supply picture per card and platform (lowest BINs, EA range, extinct)."""

    __tablename__ = "market_state"

    player_id: Mapped[int] = mapped_column(
        ForeignKey("players.id", ondelete="CASCADE"), primary_key=True
    )
    platform: Mapped[Platform] = mapped_column(str_enum(Platform), primary_key=True)
    source: Mapped[str] = mapped_column(String(32))
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime)
    # Comma-separated lowest BINs, ascending; empty when extinct.
    listings_csv: Mapped[str] = mapped_column(String(120), default="")
    range_min: Mapped[int | None]
    range_max: Mapped[int | None]
    extinct_since: Mapped[datetime | None] = mapped_column(UTCDateTime)

    @property
    def listings(self) -> tuple[int, ...]:
        return tuple(int(value) for value in self.listings_csv.split(",") if value)

    @property
    def extinct(self) -> bool:
        return not self.listings


class MarketObservation(Base):
    """History of the supply picture: one row per observation (lowest BINs, EA maximum)."""

    __tablename__ = "market_observations"
    __table_args__ = (
        Index("ix_market_observations_player_id_observed_at", "player_id", "observed_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id", ondelete="CASCADE"))
    platform: Mapped[Platform] = mapped_column(str_enum(Platform))
    source: Mapped[str] = mapped_column(String(32))
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime)
    listings_csv: Mapped[str] = mapped_column(String(120), default="")
    range_max: Mapped[int | None]

    @property
    def listings(self) -> tuple[int, ...]:
        return tuple(int(value) for value in self.listings_csv.split(",") if value)


class RadarCard(Base):
    """A card in the market scanner's universe (from FUTBIN lists), priced every few hours."""

    __tablename__ = "radar_cards"

    futbin_ref: Mapped[str] = mapped_column(String(200), primary_key=True)
    list_name: Mapped[str] = mapped_column(String(16))  # popular, latest, totw
    player_id: Mapped[int | None] = mapped_column(ForeignKey("players.id", ondelete="CASCADE"))
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    player: Mapped[Player | None] = relationship()


class UsageObservation(Base):
    """History of FUTBIN's games-played counter of a card (configured platform)."""

    __tablename__ = "usage_observations"
    __table_args__ = (
        Index("ix_usage_observations_player_id_observed_at", "player_id", "observed_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id", ondelete="CASCADE"))
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime)
    games: Mapped[int]


class FodderPrice(Base):
    """SBC fodder index: mean of the three cheapest cards of a rating on FUTBIN."""

    __tablename__ = "fodder_prices"
    __table_args__ = (Index("ix_fodder_prices_rating_observed_at", "rating", "observed_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[Platform] = mapped_column(str_enum(Platform))
    rating: Mapped[int]
    price: Mapped[int]
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime)


class AppSetting(Base):
    """Key/value settings changed in the dashboard; they override environment defaults."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(500))


class RealMatch(Base):
    """A real-life match (e.g. from OpenLigaDB) with its goals as JSON."""

    __tablename__ = "real_matches"
    __table_args__ = (
        Index("ix_real_matches_kickoff", "kickoff"),
        UniqueConstraint("source", "ext_id", name="uq_real_matches_source_ext_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32))
    ext_id: Mapped[int]
    league: Mapped[str] = mapped_column(String(16))
    season: Mapped[int]
    matchday: Mapped[int]
    kickoff: Mapped[datetime] = mapped_column(UTCDateTime)
    home_id: Mapped[int]
    home: Mapped[str] = mapped_column(String(100))
    away_id: Mapped[int]
    away: Mapped[str] = mapped_column(String(100))
    finished: Mapped[bool]
    home_goals: Mapped[int | None]
    away_goals: Mapped[int | None]
    goals_json: Mapped[str] = mapped_column(default="[]")


class ExternalCard(Base):
    """Cache: which FC card belongs to a real-life player (looked up once on FUTBIN)."""

    __tablename__ = "external_cards"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)  # e.g. "bl1:18331"
    futbin_ref: Mapped[str | None] = mapped_column(String(200))
    ea_id: Mapped[int | None]
    checked_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class TotwPrediction(Base):
    __tablename__ = "totw_predictions"

    week: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    rank: Mapped[int]
    name: Mapped[str] = mapped_column(String(100))
    team: Mapped[str] = mapped_column(String(100))
    league: Mapped[str] = mapped_column(String(16))
    score: Mapped[float]
    goals: Mapped[int]
    reasons: Mapped[str] = mapped_column(String(200))
    ea_id: Mapped[int | None]
    futbin_ref: Mapped[str | None] = mapped_column(String(200))
    card_name: Mapped[str | None] = mapped_column(String(100))
    price: Mapped[int | None]
    chem_styles_raw: Mapped[str | None] = mapped_column(String(120))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    # Same interface as Player for the chem chips in templates.
    chem_style = None

    @property
    def chem_styles(self) -> list[tuple[str, int]]:
        return parse_chem_styles(self.chem_styles_raw)


class TotwActual(Base):
    """Players of a released TOTW (from FUTBIN), for the hit rate."""

    __tablename__ = "totw_actuals"

    week: Mapped[int] = mapped_column(primary_key=True)
    futbin_id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(120))
    name: Mapped[str] = mapped_column(String(120))


class LeakItem(Base):
    """An article from a leak/news feed, waiting to be turned into a promo or dismissed."""

    __tablename__ = "leak_items"
    __table_args__ = (
        UniqueConstraint("source", "guid", name="uq_leak_items_source_guid"),
        Index("ix_leak_items_published_at", "published_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32))
    guid: Mapped[str] = mapped_column(String(300))
    title: Mapped[str] = mapped_column(String(300))
    url: Mapped[str] = mapped_column(String(500))
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    summary: Mapped[str] = mapped_column(String(1000), default="")
    is_leak: Mapped[bool] = mapped_column(default=False)
    status: Mapped[str] = mapped_column(String(16), default="new")  # new, used, dismissed
    promo_id: Mapped[int | None] = mapped_column(ForeignKey("promos.id", ondelete="SET NULL"))
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class PoolCard(Base):
    """A card tracked because of a promo (less often than the watchlist) until `until`."""

    __tablename__ = "pool_cards"

    player_id: Mapped[int] = mapped_column(
        ForeignKey("players.id", ondelete="CASCADE"), primary_key=True
    )
    promo_id: Mapped[int | None] = mapped_column(ForeignKey("promos.id", ondelete="CASCADE"))
    reason: Mapped[str] = mapped_column(String(200))
    added_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    until: Mapped[datetime] = mapped_column(UTCDateTime)

    player: Mapped[Player] = relationship()


class CardPair(Base):
    """A card and its holographic version (None if checked and none exists yet)."""

    __tablename__ = "card_pairs"

    player_id: Mapped[int] = mapped_column(
        ForeignKey("players.id", ondelete="CASCADE"), primary_key=True
    )
    holo_player_id: Mapped[int | None] = mapped_column(
        ForeignKey("players.id", ondelete="SET NULL")
    )
    checked_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    player: Mapped[Player] = relationship(foreign_keys=[player_id])
    holo: Mapped[Player | None] = relationship(foreign_keys=[holo_player_id])
